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
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from agent_interface import AgentDecision, BaseAgent, DriverState, Offer, ShiftState, WeatherEvent
from city_graph import CityGraphProvider, get_city_graph_provider, peek_city_graph_provider
from greedy_agent import GreedyAgent
from order_generator import OrderGenerator
from risk_averse_agent import RiskAverseAgent
from risk_model import (
    ACCIDENT_HOTSPOTS,
    FLOOD_PRONE_ZONES,
    SAFETY_HARD_RISK_LIMIT,
    average_risk_along_path,
    risk_bonus_points,
)
from weather_service import fetch_current_weather

logger = logging.getLogger("the_courier.server")
logging.basicConfig(level=logging.INFO)

# Coordenada del "hub" de despacho (zona céntrica de Monterrey, cerca de la
# Macroplaza) usada como punto de partida idéntico para ambos agentes.
DEPOT_LATITUDE = 25.6714
DEPOT_LONGITUDE = -100.3092

MAX_RECENT_DECISIONS_KEPT = 20
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Construir la matriz de costos Te+alpha*Re para el batching multi-pedido es
# O(N^2) Dijkstra REALES sobre la malla vial (N = 1 + 2*pedidos_activos).
# Medido sobre el grafo real de 6 km: ~4 segundos con solo 6 pedidos activos
# (13 nodos, 156 pares). Un turno largo que deja acumular más pedidos
# aceptados de los que el repartidor alcanza a servir puede estancar el
# tick varios segundos -- justo el "se queda pensando" que reportó el
# usuario. Un repartidor tampoco carga pedidos ilimitados de cualquier
# forma, así que optimizar solo entre los pedidos MÁS URGENTES (due_time
# más próximo) cuando hay demasiados activos es una simplificación
# realista, no solo una salida de emergencia.
MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING = 3

# Tope absoluto de tiempo para planificar la ruta (matriz + plan_route) de
# UN tick. Si algo tarda más que esto -- carga inusual, un caso límite no
# previsto -- se cae a FIFO en vez de dejar el turno entero esperando
# indefinidamente. No hay forma de cancelar de verdad un cómputo ya
# despachado a un hilo (`asyncio.to_thread`), así que esto es una red de
# seguridad para que la SIMULACIÓN avance, no una garantía de que ese
# hilo huérfano deje de trabajar en el acto.
ROUTE_PLANNING_TIMEOUT_SEC = 3.0

# Construidos a partir de la MISMA fuente que usa `risk_model.py` para
# calcular el riesgo de verdad (antes esto era una lista fabricada aparte,
# desincronizada de lo que el modelo realmente usaba para decidir) -- ver
# las citas de datos oficiales en risk_model.py junto a `FLOOD_PRONE_ZONES`
# y `ACCIDENT_HOTSPOTS`.
FLOOD_ZONE_PAYLOAD = [
    {
        "name": name,
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "base_severity": base_severity,
    }
    for name, lat, lon, radius_km, base_severity in FLOOD_PRONE_ZONES
]

ACCIDENT_HOTSPOT_PAYLOAD = [
    {
        "name": name,
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "base_severity": base_severity,
        "accident_count": accident_count,
    }
    for name, lat, lon, radius_km, base_severity, accident_count in ACCIDENT_HOTSPOTS
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
        200, ge=200, le=10000,
        description=(
            "Tiempo máximo para calcular una decisión sobre la malla vial real. "
            "200 ms es el límite duro que exige el reto; se permite subirlo (hasta "
            "10s) solo para depuración local, nunca como default de producción."
        ),
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
    # Se incrementa en cada `apply_event`. Los motores de simulación lo usan
    # para detectar "algo cambió desde que calculé mi ruta actual" sin tener
    # que comparar listas/severidades a mano, y así decidir si deben
    # re-rutear al repartidor que ya está en camino.
    version: int = 0

    def apply_event(self, event: WeatherEvent, severity: float) -> None:
        clamped_severity = max(0.0, min(severity, 1.0))
        self.version += 1
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
        self._travel_to_pickup_sec: float = 0.0
        self._route_nodes: List[int] = []
        self._current_route: List[dict] = []
        # Riesgo promedio medido sobre la ruta FÍSICA que de verdad se está
        # recorriendo para el pedido en curso (recalculado en
        # `_commit_route_to_offer` cada vez que se traza o retraza la
        # ruta). Es lo que decide el Bonus de Riesgo real al completar el
        # pedido -- no el riesgo estimado al momento de aceptar la oferta,
        # que puede quedar desactualizado si el entorno cambió a mitad de
        # camino.
        self._current_order_risk: Optional[float] = None
        self._recent_decisions: List[AgentDecision] = []
        # Versión de `LiveEnvironment` vigente cuando se calculó la ruta
        # física actual. Si el entorno cambia (evento sorpresa inyectado)
        # mientras hay un pedido en curso, se dispara un re-ruteo.
        self._route_environment_version: int = -1

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

        self._finalize_shift_points()
        yield self._build_snapshot(self._current_shift_state(elapsed_sec), shift_ended=True)

    def _finalize_shift_points(self) -> None:
        """Cierre determinista del turno: con aceptación obligatoria y sin
        ningún filtro de admisión, es esperable que queden pedidos en
        `active_orders` sin entregar cuando se acaba el tiempo -- su valor
        en puntos nunca se cobró y se expone aquí como KPI ('Puntos no
        obtenidos por falta de tiempo'). Aritmética simple sobre datos que
        YA están en memoria (ningún Dijkstra, ninguna llamada a
        `CityGraphProvider`) para que el cierre del turno sea determinista
        y no pueda bloquear ni colgar el event loop."""
        self.driver_state.points_lost_timeout = round(
            sum(order.base_points for order in self.driver_state.active_orders), 2
        )

    async def _tick(self, elapsed_sec: int) -> dict:
        shift_state = self._current_shift_state(elapsed_sec)

        pending_order_ids = []
        for order_dict in self._order_generator.get_pending_orders(elapsed_sec):
            order_id = order_dict["order_id"]
            if order_id in self._seen_order_ids:
                continue
            self._seen_order_ids.add(order_id)
            pending_order_ids.append(order_id)
        # Secuencial a propósito, NO `asyncio.gather`: evaluar una oferta es
        # trabajo CPU-bound (Dijkstra sobre la malla vial real), y NetworkX
        # es Python puro sujeto al GIL. "Paralelizar" con asyncio.gather no
        # las corre de verdad en paralelo -- solo hace que 3-4 ofertas se
        # turnen el mismo núcleo, y cada una individualmente termina
        # tardando varias veces más de lo que tardaría sola (medido: hasta
        # ~200ms cuando llegan 4 pedidos en el mismo tick, contra ~20-50ms
        # evaluando una por una). Con el límite duro de 200ms del reto,
        # esa auto-competencia disparaba timeouts reales que no tenían nada
        # que ver con la malla vial ni el riesgo, solo con haber lanzado
        # varias ofertas a la vez. El motor Greedy y el Risk-Averse siguen
        # corriendo en paralelo ENTRE SÍ (son tareas asyncio separadas); acá
        # solo se serializan las ofertas DENTRO de un mismo motor.
        for order_id in pending_order_ids:
            await self._evaluate_new_offer(order_id, shift_state)

        if self._current_order is None and self.driver_state.active_orders:
            await self._advance_route_plan(shift_state)
        elif self._current_order is not None and self._should_reroute():
            await self._reroute_current_order(shift_state)

        if self._current_order is not None:
            self._remaining_time_sec -= self._config.tick_interval_sec
            self._advance_visual_position(self._config.tick_interval_sec)
            if self._remaining_time_sec <= 0:
                self._complete_current_order(completed_at_sec=elapsed_sec)

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
        """Aceptación OBLIGATORIA: toda oferta entra a `active_orders` sin
        excepción, sin cupo máximo y sin importar el riesgo que mida el
        agente. Lo único que puede variar aquí es qué tan rápido calculó el
        agente su puntuación informativa -- nunca si la oferta se queda o
        no."""
        offer = self._order_generator.get_offer(order_id)

        started_at = time.perf_counter()
        try:
            decision = await asyncio.wait_for(
                self.agent.evaluate_offer(
                    offer, self.driver_state, shift_state),
                timeout=self._config.decision_timeout_ms / 1000.0,
            )
            decision.decision_latency_ms = round(
                (time.perf_counter() - started_at) * 1000.0, 1)
        except asyncio.TimeoutError:
            # Ni siquiera una decisión lenta puede rechazar la oferta bajo
            # aceptación obligatoria -- se acepta igual (entra a la cola) y
            # solo se registra que esta decisión en particular raspó el
            # límite duro de 200 ms, para el KPI `timeouts_incurred`. El
            # riesgo/bonus real de este pedido se recalcula de todos modos
            # sobre la ruta física que el motor de verdad recorra para
            # entregarlo (ver `_commit_route_to_offer`).
            self.driver_state.timeouts_incurred += 1
            decision = AgentDecision(
                order_id=order_id,
                score=0.0,
                estimated_travel_time_sec=0.0,
                reasoning=(
                    f"Accepted (mandatory acceptance): the risk/points computation "
                    f"did not finish within {self._config.decision_timeout_ms} ms; "
                    "the order is queued anyway and the real risk is recomputed "
                    "when the route is planned."
                ),
                timed_out=True,
                decision_latency_ms=float(self._config.decision_timeout_ms),
            )

        self._recent_decisions.append(decision)
        if len(self._recent_decisions) > MAX_RECENT_DECISIONS_KEPT:
            self._recent_decisions.pop(0)

        self.driver_state.active_orders.append(offer)

    async def _advance_route_plan(self, shift_state: ShiftState) -> None:
        active_orders = self.driver_state.active_orders
        prepare_route_matrix = getattr(self.agent, "prepare_route_matrix", None)

        # Optimizar entre TODOS los pedidos activos es O(N^2) Dijkstra reales
        # (ver MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING más arriba); si se
        # acumularon más de los que conviene planificar de una vez, nos
        # quedamos solo con los más urgentes para esta ronda -- el resto se
        # reconsidera en la siguiente replanificación, cuando se complete el
        # pedido en curso.
        batch_orders = active_orders
        if prepare_route_matrix is not None and len(active_orders) > MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING:
            batch_orders = sorted(active_orders, key=lambda o: o.due_time_sec)[
                :MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING
            ]

        try:
            if prepare_route_matrix is not None and len(batch_orders) > 1:
                await asyncio.wait_for(
                    prepare_route_matrix(self.driver_state, batch_orders, shift_state),
                    timeout=ROUTE_PLANNING_TIMEOUT_SEC,
                )
            order_sequence = await asyncio.wait_for(
                asyncio.to_thread(self.agent.plan_route, self.driver_state, batch_orders),
                timeout=ROUTE_PLANNING_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s: planificacion de ruta supero %.1fs con %d pedidos en el lote; "
                "usando FIFO de emergencia para no detener el turno.",
                self.agent.name, ROUTE_PLANNING_TIMEOUT_SEC, len(batch_orders),
            )
            order_sequence = [o.order_id for o in batch_orders]

        # Red de seguridad: un `plan_route` puede legítimamente devolver
        # menos pedidos de los que hay en cola (ruteo orientado a puntos),
        # pero NUNCA debe dejar al repartidor detenido teniendo pedidos sin
        # entregar. Si la secuencia sale vacía y todavía hay cola, se
        # atiende en orden de urgencia: entregar tarde sigue cobrando los
        # puntos base y se reporta en el KPI "Tarde", que siempre es mejor
        # que quedarse parado sin cobrar nada.
        if not order_sequence:
            if not batch_orders:
                return
            order_sequence = [
                o.order_id for o in sorted(batch_orders, key=lambda o: o.due_time_sec)
            ]
            logger.info(
                "%s: la planificacion no selecciono ningun pedido con %d en cola; "
                "se atiende el mas urgente para no dejar al repartidor detenido.",
                self.agent.name, len(batch_orders),
            )

        next_order_id = order_sequence[0]
        offer = next(o for o in active_orders if o.order_id == next_order_id)
        await self._commit_route_to_offer(
            offer, shift_state, from_node=self.driver_state.current_node
        )

    def _should_reroute(self) -> bool:
        """Un agente ciego al riesgo (Greedy) no expone `risk_weighted_path`
        y nunca re-rutea: es fiel a su diseño. El agente inteligente sí lo
        hace en cuanto el entorno cambió desde que trazó la ruta actual
        (evento sorpresa inyectado a mitad de un pedido en curso)."""
        if getattr(self.agent, "risk_weighted_path", None) is None:
            return False
        return self._route_environment_version != self._environment.version

    async def _reroute_current_order(self, shift_state: ShiftState) -> None:
        offer = self._current_order
        assert offer is not None
        already_picked_up = self._route_elapsed_sec >= self._travel_to_pickup_sec
        await self._commit_route_to_offer(
            offer,
            shift_state,
            from_node=self.driver_state.current_node,
            already_picked_up=already_picked_up,
        )

    async def _route_path(
        self, origin_node: int, dest_node: int, shift_state: ShiftState
    ) -> List[int]:
        """Secuencia de nodos entre dos puntos. Usa `risk_weighted_path` del
        agente cuando está disponible (Te + alpha*Re por arista), y cae de
        vuelta al camino más rápido a secas para agentes ciegos al riesgo
        (Greedy) o cuando alpha es 0."""
        risk_weighted_path = getattr(self.agent, "risk_weighted_path", None)
        if risk_weighted_path is not None:
            path = await risk_weighted_path(origin_node, dest_node, shift_state)
        else:
            path = await self._graph_provider.shortest_path(origin_node, dest_node)
        return list(path)

    async def _commit_route_to_offer(
        self,
        offer: Offer,
        shift_state: ShiftState,
        from_node: int,
        already_picked_up: bool = False,
    ) -> None:
        """Calcula (o recalcula) la ruta física hacia `offer` desde
        `from_node` y la deja lista para que `_advance_visual_position` la
        recorra. Se usa tanto para arrancar un pedido nuevo como para
        re-rutear uno en curso cuando el entorno cambió."""
        if already_picked_up:
            pickup_path: List[int] = [from_node]
            travel_to_pickup_sec = 0.0
        else:
            pickup_path = await self._route_path(from_node, offer.pickup_node, shift_state)
            travel_to_pickup_sec = await self._graph_provider.path_travel_time_sec(
                tuple(pickup_path)
            )

        dropoff_origin = from_node if already_picked_up else offer.pickup_node
        dropoff_path = await self._route_path(dropoff_origin, offer.dropoff_node, shift_state)
        travel_to_dropoff_sec = await self._graph_provider.path_travel_time_sec(
            tuple(dropoff_path)
        )

        route_nodes = pickup_path + dropoff_path[1:]
        route_coordinates = await self._graph_provider.route_coordinates(tuple(route_nodes))
        self._current_route = [
            {"lat": latitude, "lon": longitude}
            for latitude, longitude in route_coordinates
        ]
        self._route_nodes = list(route_nodes)
        self._travel_to_pickup_sec = max(travel_to_pickup_sec, 0.0)
        self._route_travel_sec = max(travel_to_pickup_sec + travel_to_dropoff_sec, 0.0)
        self._route_elapsed_sec = 0.0
        self._route_environment_version = self._environment.version
        # Riesgo REAL de la ruta física que se acaba de trazar (no el
        # estimado al aceptar la oferta): muestreado sobre `route_nodes`
        # directamente, sin volver a correr Dijkstra -- son los mismos
        # nodos que el repartidor de verdad va a recorrer y que
        # `_advance_visual_position` ya usa para animarlo.
        self._current_order_risk = average_risk_along_path(
            self._graph_provider, tuple(route_nodes), shift_state
        )

        self._current_order = offer
        self._remaining_time_sec = (
            travel_to_pickup_sec + travel_to_dropoff_sec + offer.service_time_sec
        )

    def _complete_current_order(self, completed_at_sec: int) -> None:
        offer = self._current_order
        assert offer is not None

        # Puntos Base Obtenidos: se cobran al ENTREGAR, no al aceptar -- un
        # pedido que quede huérfano en `active_orders` al cerrar el turno
        # nunca pasa por aquí (ver `_finalize_shift_points`).
        self.driver_state.points_base = round(
            self.driver_state.points_base + offer.base_points, 2
        )

        # Bonus de Riesgo: sobre el riesgo REAL medido de la ruta física que
        # se acaba de recorrer (`_current_order_risk`), no el estimado al
        # momento de aceptar la oferta -- así un reruteo a mitad de camino
        # (evento sorpresa) se refleja en el bonus que de verdad se cobra.
        weighted_risk = self._current_order_risk
        if weighted_risk is not None:
            self.driver_state.points_risk_bonus = round(
                self.driver_state.points_risk_bonus + risk_bonus_points(weighted_risk), 2
            )
            self.driver_state.total_risk_sum += weighted_risk
            self.driver_state.total_risk_count += 1
            if weighted_risk >= SAFETY_HARD_RISK_LIMIT:
                self.driver_state.risky_orders_handled += 1

        self.driver_state.distance_traveled_km = round(
            self.driver_state.distance_traveled_km + offer.straight_line_distance_km, 2
        )
        self.driver_state.orders_completed += 1
        # Se aceptó dentro de su ventana (o ya no se ofrecería), pero puede
        # que para cuando de verdad le tocó turno en la cola ya haya
        # vencido -- eso es una entrega tarde de verdad, y se cuenta en vez
        # de tratarla igual que una a tiempo (antes no había forma de
        # distinguirlas, así que un pedido entregado 26 minutos tarde se
        # veía idéntico a uno puntual en todos los KPIs).
        if completed_at_sec > offer.due_time_sec:
            self.driver_state.orders_delivered_late += 1
        self.driver_state.current_node = offer.dropoff_node
        self.driver_state.active_orders = [
            o for o in self.driver_state.active_orders if o.order_id != offer.order_id
        ]
        self._current_order = None
        self._current_order_risk = None
        self._remaining_time_sec = 0.0
        self._route_travel_sec = 0.0
        self._route_elapsed_sec = 0.0
        self._travel_to_pickup_sec = 0.0
        self._route_nodes = []
        self._current_route = []

    def _advance_visual_position(self, elapsed_sec: float) -> None:
        """Avanza `driver_state.current_node` a lo largo de `_route_nodes`
        proporcionalmente al tiempo transcurrido en este tick, para que el
        repartidor se vea moverse tramo a tramo mientras dura el pedido en
        vez de teletransportarse al dropoff cuando termina."""
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
        # Le dice al frontend si el pedido en curso ya fue recogido (viajando
        # al dropoff) o si el repartidor todavía va en camino al pickup --
        # así el mapa puede animar el punto de recogida en el momento exacto
        # en que se "levanta" el paquete, no solo cuando se acepta la oferta.
        pickup_reached = (
            self._route_elapsed_sec >= self._travel_to_pickup_sec
            if self._current_order is not None
            else None
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
            "pickup_reached": pickup_reached,
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
        "accident_hotspots": ACCIDENT_HOTSPOT_PAYLOAD,
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

    # El turno arranca con el clima REAL de Monterrey en este instante
    # (Open-Meteo, ver weather_service.py), no despejado por default. Los
    # botones de "Evento sorpresa" del panel siguen funcionando igual por
    # encima de esto -- son para disparar algo dramático a mitad de turno
    # bajo demanda, no para simular el clima real (eso ya lo hace esto).
    # Si la API no responde, `fetch_current_weather` devuelve `None` y el
    # turno arranca en CLEAR sin bloquearse esperando la red.
    real_weather = await fetch_current_weather(DEPOT_LATITUDE, DEPOT_LONGITUDE)
    if real_weather is not None and real_weather.event != WeatherEvent.CLEAR:
        environment.apply_event(real_weather.event, real_weather.severity)

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
            "accident_hotspots": ACCIDENT_HOTSPOT_PAYLOAD,
            "real_weather": (
                {
                    "event_type": real_weather.event.value,
                    "severity": real_weather.severity,
                    "temperature_c": real_weather.temperature_c,
                    "precipitation_mm": real_weather.precipitation_mm,
                    "wind_speed_kph": real_weather.wind_speed_kph,
                    "description": real_weather.description,
                    "source": real_weather.source,
                }
                if real_weather is not None
                else None
            ),
        }
    )

    snapshot_queue: "asyncio.Queue[dict]" = asyncio.Queue()

    async def pump_engine(engine: ShiftSimulatorEngine) -> None:
        async for snapshot in engine.run():
            await snapshot_queue.put(snapshot)
        await snapshot_queue.put({"type": "agent_finished", "agent_name": engine.agent.name})

    async def forward_snapshots_to_client() -> None:
        # Empareja los snapshots de los dos motores por elapsed_sec antes de
        # mandarlos: si uno se transmitiera en cuanto llega, un motor podria
        # adelantarse varios ticks al otro en tiempo real (uno tarda mas
        # que el otro en decidir/rerutear ese tick en particular) y el
        # split-screen se veria "desincronizado" -- justo el "no se
        # reproducen a la par" reportado antes.
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
        # OJO: no basta un solo `asyncio.wait(..., FIRST_COMPLETED)` sobre
        # TODAS las tareas. Los dos motores casi nunca terminan su
        # `pump_engine` en el mismo instante real -- uno tarda más que el
        # otro por tick, sobre todo el inteligente cuando replanifica ruta
        # -- así que el que terminaba primero (con su turno completo, sin
        # ningún error) disparaba igual el FIRST_COMPLETED de un `wait` de
        # una sola pasada, y el `finally` de abajo cancelaba TODO de
        # inmediato: el OTRO motor a la mitad de su turno, y
        # `forwarding_task` antes de que alcanzara a mandar los últimos
        # ticks emparejados o el "session_complete". Eso era exactamente
        # la sesión "parándose sola": terminaba en cuanto CUALQUIERA de
        # los dos agentes acababa su turno, no cuando de verdad terminaban
        # los dos. Ahora se sigue esperando mientras lo único que termina
        # son motores SIN error (esperado y normal): la sesión de verdad
        # concluye solo cuando el cliente pide parar o se desconecta
        # (`command_task`), cuando `forwarding_task` ya emparejó y mandó
        # TODOS los ticks de ambos motores más `session_complete`, o
        # cuando algo truena de verdad.
        remaining = set(all_tasks)
        while True:
            done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            should_stop = forwarding_task in done or command_task in done
            for task in done:
                exception = task.exception()
                if exception is not None and not isinstance(exception, WebSocketDisconnect):
                    raise exception
            if should_stop or not remaining:
                break
    except WebSocketDisconnect:
        logger.info("Cliente desconectado de la sesión %s.", session_id)
    finally:
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        async with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
