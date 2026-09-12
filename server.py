"""
server.py  (PASO 4)
=====================
Servidor FastAPI 100% asíncrono para el simulador de turno de "The Courier".

Expone:
- `WS  /ws/shift-simulation`: arranca una sesión de simulación con un
  `random_seed` dado y transmite, en tiempo real, el estado de DOS motores
  de simulación corriendo en paralelo (Agente Base vs Agente Inteligente)
  para renderizarse lado a lado (split-screen) en el frontend.
- `POST /sessions/{session_id}/events`: inyecta en vivo un "evento sorpresa"
  (p. ej. HEAVY_RAIN) sobre una sesión activa, vía REST.
- El mismo WebSocket también acepta mensajes entrantes `{"type":
  "inject_event", ...}` para inyectar eventos sin salir del canal en vivo.

Reglas de arquitectura aplicadas
---------------------------------
- Cero I/O síncrono bloqueante: toda interacción con OSMnx/NetworkX pasa por
  `CityGraphProvider` (que internamente usa `asyncio.to_thread`); el ruteo
  con OR-Tools del agente inteligente también se despacha a un hilo.
- Timeout duro de 200 ms en cada decisión de agente: se aplica aquí, en el
  motor de simulación, con `asyncio.wait_for`, y es la ÚNICA fuente de
  verdad sobre el rechazo automático por latencia.
- Determinismo: los dos motores comparten la MISMA instancia de
  `OrderGenerator` (mismo `random_seed`), por lo que reciben exactamente el
  mismo turno no visto; cada motor mantiene su propio estado de qué
  pedidos ya evaluó, para que la competencia sea justa (uno no le "roba"
  pedidos al otro).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from agent_interface import AgentDecision, BaseAgent, DriverState, Offer, ShiftState, WeatherEvent
from city_graph import CityGraphProvider, get_city_graph_provider, peek_city_graph_provider
from greedy_agent import GreedyAgent
from order_generator import OrderGenerator
from risk_averse_agent import RiskAverseAgent

logger = logging.getLogger("the_courier.server")
logging.basicConfig(level=logging.INFO)

# Coordenada del "hub" de despacho (zona céntrica de Monterrey, cerca de la
# Macroplaza) usada como punto de partida idéntico para ambos agentes.
DEPOT_LATITUDE = 25.6714
DEPOT_LONGITUDE = -100.3092

MAX_RECENT_DECISIONS_KEPT = 20
STATIC_DIR = Path(__file__).resolve().parent / "static"

FLOOD_ZONE_PAYLOAD = [
    {
        "name": name,
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "base_severity": base_severity,
    }
    for name, lat, lon, radius_km, base_severity in (
        ("Puente del Papa / Río Santa Catarina", 25.6690, -100.3550, 1.2, 0.9),
        ("Distribuidor Gonzalitos", 25.6825, -100.3505, 1.0, 0.8),
        ("Av. Morones Prieto bajo Puente Constitución",
         25.6640, -100.3320, 1.0, 0.85),
        ("Cruce Av. Revolución / Río Santa Catarina", 25.6555, -100.3610, 1.0, 0.75),
        ("Paso a Desnivel Cumbres", 25.7100, -100.3800, 1.3, 0.6),
    )
]


# --------------------------------------------------------------------- #
# Modelos de request/response de la API
# --------------------------------------------------------------------- #
class SimulationConfig(BaseModel):
    """Configuración inicial de una sesión de simulación, enviada como el
    primer mensaje JSON del cliente al abrir el WebSocket."""

    random_seed: int = Field(..., description="Semilla determinista del turno")
    shift_duration_sec: int = Field(
        3 * 3600, gt=0, description="Duración simulada del turno")
    num_orders: int = Field(
        40, gt=0, le=500, description="Pedidos totales a generar")
    tick_interval_sec: int = Field(
        30, gt=0, description="Paso de tiempo simulado por tick")
    time_scale: float = Field(
        20.0, gt=0, description="Segundos simulados por segundo real (velocidad de reproducción)"
    )
    risk_alpha: float = Field(
        0.35, ge=0.0, description="Alpha del Agente Inteligente")
    decision_timeout_ms: int = Field(
        2000, ge=200, le=10000,
        description="Tiempo máximo para calcular una decisión sobre la malla vial real",
    )


class InjectEventRequest(BaseModel):
    """Payload para inyectar un evento sorpresa, vía REST o vía WebSocket."""

    event_type: WeatherEvent
    severity: float = Field(0.6, ge=0.0, le=1.0)


# --------------------------------------------------------------------- #
# Entorno mutable compartido por ambos motores de una sesión
# --------------------------------------------------------------------- #
@dataclass
class LiveEnvironment:
    """Condiciones ambientales de la sesión, mutables en caliente por la
    inyección de eventos sorpresa. Ambos motores (Greedy y Risk-Averse) leen
    la MISMA instancia en cada tick, garantizando que compiten bajo
    condiciones idénticas."""

    active_events: List[WeatherEvent] = field(
        default_factory=lambda: [WeatherEvent.CLEAR])
    severity: float = 0.0
    ambient_temperature_c: float = 40.0

    def apply_event(self, event: WeatherEvent, severity: float) -> None:
        clamped_severity = max(0.0, min(severity, 1.0))
        if event == WeatherEvent.CLEAR:
            self.active_events = [WeatherEvent.CLEAR]
            self.severity = 0.0
            return

        if WeatherEvent.CLEAR in self.active_events:
            self.active_events.remove(WeatherEvent.CLEAR)
        if event not in self.active_events:
            self.active_events.append(event)
        self.severity = clamped_severity
        if event == WeatherEvent.EXTREME_HEAT:
            self.ambient_temperature_c = 40.0 + 8.0 * clamped_severity


@dataclass
class SimulationSession:
    """Agrupa los recursos compartidos de una sesión activa (una conexión
    WebSocket = una sesión = dos motores corriendo en paralelo)."""

    session_id: str
    config: SimulationConfig
    graph_provider: CityGraphProvider
    order_generator: OrderGenerator
    environment: LiveEnvironment


SESSIONS: Dict[str, SimulationSession] = {}
SESSIONS_LOCK = asyncio.Lock()


# --------------------------------------------------------------------- #
# Motor de simulación de un solo agente
# --------------------------------------------------------------------- #
class ShiftSimulatorEngine:
    """
    Ejecuta el turno completo de UN repartidor gobernado por UN agente
    (`BaseAgent`), avanzando el tiempo simulado en pasos de
    `config.tick_interval_sec` y emitiendo un snapshot de estado por tick.

    Dos instancias de este motor (una por agente) corren concurrentemente
    dentro de la misma sesión, compartiendo `order_generator` y
    `environment`, pero con su propio `DriverState` y su propio historial de
    qué pedidos ya evaluó.
    """

    def __init__(
        self,
        agent: BaseAgent,
        order_generator: OrderGenerator,
        graph_provider: CityGraphProvider,
        environment: LiveEnvironment,
        config: SimulationConfig,
        session_id: str,
        depot_node: int,
    ) -> None:
        self.agent = agent
        self._order_generator = order_generator
        self._graph_provider = graph_provider
        self._environment = environment
        self._config = config
        self._session_id = session_id

        self.driver_state = DriverState(
            current_node=depot_node, current_time_sec=0)
        self._seen_order_ids: set[int] = set()
        self._current_order: Optional[Offer] = None
        self._remaining_time_sec: float = 0.0
        self._route_travel_sec: float = 0.0
        self._route_elapsed_sec: float = 0.0
        self._route_nodes: List[int] = []
        self._current_route: List[dict] = []
        self._recent_decisions: List[AgentDecision] = []

    async def run(self):
        """Async generator: avanza el turno tick a tick y produce un
        snapshot serializable después de cada uno, hasta agotar
        `shift_duration_sec`."""
        elapsed_sec = 0
        while elapsed_sec < self._config.shift_duration_sec:
            elapsed_sec = min(
                elapsed_sec + self._config.tick_interval_sec, self._config.shift_duration_sec
            )
            snapshot = await self._tick(elapsed_sec)
            yield snapshot
            await asyncio.sleep(self._config.tick_interval_sec / self._config.time_scale)

        yield self._build_snapshot(self._current_shift_state(elapsed_sec), shift_ended=True)

    async def _tick(self, elapsed_sec: int) -> dict:
        shift_state = self._current_shift_state(elapsed_sec)

        pending_order_ids = []
        for order_dict in self._order_generator.get_pending_orders(elapsed_sec):
            order_id = order_dict["order_id"]
            if order_id in self._seen_order_ids:
                continue
            self._seen_order_ids.add(order_id)
            pending_order_ids.append(order_id)
        if pending_order_ids:
            await asyncio.gather(*(
                self._evaluate_new_offer(order_id, shift_state)
                for order_id in pending_order_ids
            ))

        if self._current_order is None and self.driver_state.active_orders:
            await self._advance_route_plan()

        if self._current_order is not None:
            self._remaining_time_sec -= self._config.tick_interval_sec
            self._advance_visual_position(self._config.tick_interval_sec)
            if self._remaining_time_sec <= 0:
                self._complete_current_order()

        self.driver_state.current_time_sec = elapsed_sec
        return self._build_snapshot(shift_state, shift_ended=False)

    def _current_shift_state(self, elapsed_sec: int) -> ShiftState:
        return ShiftState(
            shift_id=self._session_id,
            random_seed=self._config.random_seed,
            elapsed_sec=elapsed_sec,
            shift_duration_sec=self._config.shift_duration_sec,
            ambient_temperature_c=self._environment.ambient_temperature_c,
            active_events=list(self._environment.active_events),
            event_severity=self._environment.severity,
        )

    async def _evaluate_new_offer(self, order_id: int, shift_state: ShiftState) -> None:
        offer = self._order_generator.get_offer(order_id)
        try:
            decision = await asyncio.wait_for(
                self.agent.evaluate_offer(
                    offer, self.driver_state, shift_state),
                timeout=self._config.decision_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            self.driver_state.timeouts_incurred += 1
            decision = AgentDecision(
                order_id=order_id,
                accepted=False,
                score=0.0,
                estimated_travel_time_sec=0.0,
                reasoning=(
                    f"Timeout: el agente no respondió en {self._config.decision_timeout_ms} ms; "
                    "la oferta se rechaza automáticamente por seguridad."
                ),
                timed_out=True,
            )

        self._recent_decisions.append(decision)
        if len(self._recent_decisions) > MAX_RECENT_DECISIONS_KEPT:
            self._recent_decisions.pop(0)

        if decision.accepted:
            self.driver_state.active_orders.append(offer)
        else:
            self.driver_state.orders_rejected += 1

    async def _advance_route_plan(self) -> None:
        active_orders = self.driver_state.active_orders
        order_sequence = await asyncio.to_thread(
            self.agent.plan_route, self.driver_state, active_orders
        )
        if not order_sequence:
            return

        next_order_id = order_sequence[0]
        offer = next(o for o in active_orders if o.order_id == next_order_id)

        travel_to_pickup_sec = await self._graph_provider.travel_time_sec(
            self.driver_state.current_node, offer.pickup_node
        )
        travel_to_dropoff_sec = await self._graph_provider.travel_time_sec(
            offer.pickup_node, offer.dropoff_node
        )
        pickup_path = await self._graph_provider.shortest_path(
            self.driver_state.current_node, offer.pickup_node
        )
        dropoff_path = await self._graph_provider.shortest_path(
            offer.pickup_node, offer.dropoff_node
        )
        route_nodes = pickup_path + dropoff_path[1:]
        route_coordinates = await self._graph_provider.route_coordinates(route_nodes)
        self._current_route = [
            {"lat": latitude, "lon": longitude}
            for latitude, longitude in route_coordinates
        ]
        self._route_nodes = list(route_nodes)
        self._route_travel_sec = max(
            travel_to_pickup_sec + travel_to_dropoff_sec, 0.0
        )
        self._route_elapsed_sec = 0.0

        self._current_order = offer
        self._remaining_time_sec = (
            travel_to_pickup_sec + travel_to_dropoff_sec + offer.service_time_sec
        )

    def _complete_current_order(self) -> None:
        offer = self._current_order
        assert offer is not None

        self.driver_state.earnings_mxn = round(
            self.driver_state.earnings_mxn + offer.base_fare_mxn, 2
        )
        self.driver_state.distance_traveled_km = round(
            self.driver_state.distance_traveled_km + offer.straight_line_distance_km, 2
        )
        self.driver_state.orders_completed += 1
        self.driver_state.current_node = offer.dropoff_node
        self.driver_state.active_orders = [
            o for o in self.driver_state.active_orders if o.order_id != offer.order_id
        ]
        self._current_order = None
        self._remaining_time_sec = 0.0
        self._route_travel_sec = 0.0
        self._route_elapsed_sec = 0.0
        self._route_nodes = []
        self._current_route = []

    def _advance_visual_position(self, elapsed_sec: float) -> None:
        if not self._route_nodes or self._route_travel_sec <= 0:
            return
        self._route_elapsed_sec = min(
            self._route_elapsed_sec + elapsed_sec, self._route_travel_sec
        )
        progress = self._route_elapsed_sec / self._route_travel_sec
        route_index = min(
            int(progress * (len(self._route_nodes) - 1)),
            len(self._route_nodes) - 1,
        )
        self.driver_state.current_node = self._route_nodes[route_index]

    def _build_snapshot(self, shift_state: ShiftState, shift_ended: bool) -> dict:
        driver_lat, driver_lon = self._graph_provider.node_coordinates(
            self.driver_state.current_node
        )
        return {
            "type": "shift_ended" if shift_ended else "tick",
            "agent_name": self.agent.name,
            "alpha": self.agent.alpha,
            "driver_state": self.driver_state.model_dump(),
            "driver_position": {"lat": driver_lat, "lon": driver_lon},
            "shift_state": shift_state.model_dump(),
            "current_order_in_progress": (
                self._current_order.model_dump() if self._current_order else None
            ),
            "current_route": self._current_route,
            "recent_decisions": [d.model_dump() for d in self._recent_decisions[-5:]],
        }


# --------------------------------------------------------------------- #
# Aplicación FastAPI
# --------------------------------------------------------------------- #
app = FastAPI(
    title="The Courier — Simulador de Turno (HackMTY 2026 / Infosys)",
    description=(
        "Simulador asíncrono de repartidores en Monterrey que compara, en "
        "paralelo, un Agente Base (Greedy) contra un Agente Inteligente "
        "adverso al riesgo (Te + alpha * Re) sobre la malla vial real."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def prewarm_city_graph() -> None:
    """Dispara la carga del grafo vial de Monterrey en segundo plano tan
    pronto arranca el servidor, para que la primera sesión de simulación no
    absorba el costo completo de la descarga de OSMnx."""
    asyncio.create_task(get_city_graph_provider())


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "active_sessions": len(SESSIONS)}


@app.get("/meta")
async def simulation_meta() -> dict:
    """Metadatos estáticos para el panel visual (depósito, zonas de riesgo)."""
    graph_ready = False
    bounds_payload = None
    provider = peek_city_graph_provider()
    if provider is not None and provider.is_loaded:
        graph_ready = True
        bounds = provider.bounds
        bounds_payload = {
            "min_lat": bounds.min_lat,
            "max_lat": bounds.max_lat,
            "min_lon": bounds.min_lon,
            "max_lon": bounds.max_lon,
        }

    return {
        "depot": {"lat": DEPOT_LATITUDE, "lon": DEPOT_LONGITUDE},
        "flood_zones": FLOOD_ZONE_PAYLOAD,
        "graph_ready": graph_ready,
        "bounds": bounds_payload,
    }


@app.get("/")
async def visual_lab() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/sessions/{session_id}/events")
async def inject_event_rest(session_id: str, request: InjectEventRequest) -> dict:
    """Inyecta en vivo un evento sorpresa sobre una sesión de simulación
    activa, vía REST (alternativa al mensaje WebSocket equivalente)."""
    async with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"Sesión '{session_id}' no encontrada o ya finalizada.")

    session.environment.apply_event(request.event_type, request.severity)
    return {
        "status": "ok",
        "session_id": session_id,
        "event_type": request.event_type.value,
        "severity": request.severity,
        "active_events": [event.value for event in session.environment.active_events],
    }


@app.websocket("/ws/shift-simulation")
async def shift_simulation_ws(websocket: WebSocket) -> None:
    """
    Punto de entrada del split-screen: arranca una sesión de simulación y
    transmite, intercalados, los snapshots de los dos motores (Greedy y
    Risk-Averse) corriendo en paralelo sobre el MISMO turno no visto.

    Primer mensaje esperado del cliente: un JSON con la forma de
    `SimulationConfig`. A partir de ahí, el cliente puede enviar en
    cualquier momento:
        {"type": "inject_event", "event_type": "HEAVY_RAIN", "severity": 0.8}
        {"type": "stop"}
    """
    await websocket.accept()

    try:
        raw_config = await websocket.receive_json()
        config = SimulationConfig(**raw_config)
    except (ValidationError, ValueError, TypeError) as exc:
        await websocket.send_json({"type": "error", "detail": f"Configuración inválida: {exc}"})
        await websocket.close(code=1003)
        return
    except WebSocketDisconnect:
        return

    graph_provider = await get_city_graph_provider()
    order_generator = await OrderGenerator.create(
        random_seed=config.random_seed,
        graph_provider=graph_provider,
        num_orders=config.num_orders,
        shift_duration_sec=config.shift_duration_sec,
    )
    environment = LiveEnvironment()
    session_id = uuid4().hex
    session = SimulationSession(
        session_id=session_id,
        config=config,
        graph_provider=graph_provider,
        order_generator=order_generator,
        environment=environment,
    )

    async with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    depot_node = await graph_provider.nearest_node(DEPOT_LATITUDE, DEPOT_LONGITUDE)

    greedy_engine = ShiftSimulatorEngine(
        agent=GreedyAgent(graph_provider),
        order_generator=order_generator,
        graph_provider=graph_provider,
        environment=environment,
        config=config,
        session_id=session_id,
        depot_node=depot_node,
    )
    risk_engine = ShiftSimulatorEngine(
        agent=RiskAverseAgent(graph_provider, alpha=config.risk_alpha),
        order_generator=order_generator,
        graph_provider=graph_provider,
        environment=environment,
        config=config,
        session_id=session_id,
        depot_node=depot_node,
    )

    await websocket.send_json(
        {
            "type": "session_started",
            "session_id": session_id,
            "total_orders": order_generator.total_orders,
            "config": config.model_dump(),
            "orders": order_generator.list_orders(),
            "depot": {"lat": DEPOT_LATITUDE, "lon": DEPOT_LONGITUDE},
            "flood_zones": FLOOD_ZONE_PAYLOAD,
        }
    )

    snapshot_queue: "asyncio.Queue[dict]" = asyncio.Queue()

    async def pump_engine(engine: ShiftSimulatorEngine) -> None:
        async for snapshot in engine.run():
            await snapshot_queue.put(snapshot)
        await snapshot_queue.put({"type": "agent_finished", "agent_name": engine.agent.name})

    async def forward_snapshots_to_client() -> None:
        finished_agents = 0
        pending_ticks: dict[tuple[int, str], dict[str, dict]] = {}
        while finished_agents < 2:
            snapshot = await snapshot_queue.get()
            if snapshot.get("type") == "agent_finished":
                finished_agents += 1
                continue

            shift_state = snapshot.get("shift_state", {})
            tick_key = (
                int(shift_state.get("elapsed_sec", 0)),
                snapshot.get("type", "tick"),
            )
            tick_snapshots = pending_ticks.setdefault(tick_key, {})
            tick_snapshots[snapshot["agent_name"]] = snapshot

            if len(tick_snapshots) == 2:
                for agent_name in ("greedy_base", "risk_averse_smart"):
                    await websocket.send_json(tick_snapshots[agent_name])
                del pending_ticks[tick_key]
        await websocket.send_json({"type": "session_complete", "session_id": session_id})

    async def listen_for_client_commands() -> None:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "inject_event":
                try:
                    event_request = InjectEventRequest(
                        event_type=message["event_type"],
                        severity=message.get("severity", 0.6),
                    )
                except (ValidationError, KeyError) as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue
                environment.apply_event(
                    event_request.event_type, event_request.severity)
                await websocket.send_json(
                    {
                        "type": "event_ack",
                        "event_type": event_request.event_type.value,
                        "severity": event_request.severity,
                    }
                )
            elif message_type == "stop":
                raise WebSocketDisconnect(code=1000)

    producer_tasks = [
        asyncio.create_task(pump_engine(greedy_engine)),
        asyncio.create_task(pump_engine(risk_engine)),
    ]
    forwarding_task = asyncio.create_task(forward_snapshots_to_client())
    command_task = asyncio.create_task(listen_for_client_commands())

    all_tasks = producer_tasks + [forwarding_task, command_task]
    try:
        done, _pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exception = task.exception()
            if exception is not None and not isinstance(exception, WebSocketDisconnect):
                raise exception
    except WebSocketDisconnect:
        logger.info("Cliente desconectado de la sesión %s.", session_id)
    finally:
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        async with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
