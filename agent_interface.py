"""
agent_interface.py
===================
Modelos de dominio compartidos y contrato abstracto (`BaseAgent`) que deben
implementar tanto el Agente Base (Greedy) como el Agente Inteligente
(Risk-Averse) del simulador "The Courier".

Todos los modelos de datos usan Pydantic para poder serializarse
directamente a JSON sobre el WebSocket de `server.py` sin capas de
traducción adicionales.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class WeatherEvent(str, Enum):
    """Eventos sorpresa que pueden inyectarse en vivo durante el turno."""

    CLEAR = "CLEAR"
    HEAVY_RAIN = "HEAVY_RAIN"
    FLASH_FLOOD = "FLASH_FLOOD"
    HEAVY_TRAFFIC = "HEAVY_TRAFFIC"
    EXTREME_HEAT = "EXTREME_HEAT"


class Offer(BaseModel):
    """Una oferta de pedido concreta, ya proyectada sobre la malla vial real
    de Monterrey (los nodos de pickup/dropoff provienen siempre de
    `CityGraphProvider.nearest_node`, nunca de coordenadas crudas)."""

    order_id: int
    pickup_node: int = Field(..., description="Nodo OSMnx del punto de recolección")
    dropoff_node: int = Field(..., description="Nodo OSMnx del punto de entrega")
    pickup_lat: float
    pickup_lon: float
    dropoff_lat: float
    dropoff_lon: float
    ready_time_sec: int = Field(..., description="Momento del turno en que el pedido aparece")
    due_time_sec: int = Field(..., description="Momento límite de entrega (ventana de tiempo)")
    service_time_sec: int = Field(..., description="Tiempo fijo de carga/descarga en el punto")
    base_fare_mxn: float = Field(..., gt=0, description="Pago bruto ofrecido por el pedido")
    straight_line_distance_km: float = Field(
        ..., description="Solo referencia informativa; NUNCA usar para decidir rutas"
    )

    class Config:
        frozen = True


class DriverState(BaseModel):
    """Estado mutable del repartidor dentro de un motor de simulación."""

    current_node: int
    current_time_sec: int
    earnings_mxn: float = 0.0
    distance_traveled_km: float = 0.0
    orders_completed: int = 0
    orders_rejected: int = 0
    orders_delivered_late: int = Field(
        0, description="De `orders_completed`, cuántos se entregaron después "
        "de su `due_time_sec` -- se aceptaron dentro de su ventana, pero para "
        "cuando les tocó turno en la cola del repartidor ya había vencido. "
        "Antes no se distinguía de una entrega puntual en ningún KPI.")
    timeouts_incurred: int = 0
    active_orders: List[Offer] = Field(default_factory=list)

    # --- KPIs de seguridad / impacto de negocio -------------------------
    # Acumulados a lo largo del turno por el motor de simulación
    # (`server.py`), a partir de lo que reporta cada `AgentDecision`. Son
    # la base numérica para justificar el modelo adverso al riesgo: cuánto
    # riesgo aceptó en promedio, y cuántos pedidos rechazó específicamente
    # por el límite duro de seguridad (proxy de "incidentes potenciales
    # evitados"), sin importar el pago ofrecido.
    accepted_risk_sum: float = Field(
        0.0, description="Suma de `estimated_risk` de cada oferta ACEPTADA")
    accepted_risk_count: int = Field(
        0, description="Cuántas ofertas aceptadas incluyeron `estimated_risk` "
        "(promedio = accepted_risk_sum / accepted_risk_count)")
    safety_rejections: int = Field(
        0, description="Ofertas rechazadas por superar el límite duro de "
        "riesgo (`hard_safety_violation`), sin importar el pago ofrecido -- "
        "0 siempre para un agente que no evalúa riesgo (p. ej. Greedy)")
    risky_orders_accepted: int = Field(
        0, description="Ofertas ACEPTADAS que superan `SAFETY_HARD_RISK_LIMIT` "
        "(`exceeds_safety_threshold`) -- para Greedy, cuenta cuántos viajes "
        "de alto riesgo tomó igual por ser ciego al riesgo; para el agente "
        "adverso al riesgo debería quedarse en 0 (los rechaza en cambio)")


class ShiftState(BaseModel):
    """Estado global del turno: condiciones ambientales compartidas por
    todas las decisiones del agente durante ese instante de la simulación."""

    shift_id: str
    random_seed: int
    elapsed_sec: int
    shift_duration_sec: int
    ambient_temperature_c: float = 40.0
    active_events: List[WeatherEvent] = Field(default_factory=lambda: [WeatherEvent.CLEAR])
    event_severity: float = Field(
        0.0, ge=0.0, le=1.0, description="Intensidad 0-1 del evento activo más severo"
    )


class AgentDecision(BaseModel):
    """Resultado estructurado de `evaluate_offer`, listo para transmitirse
    por WebSocket y para auditar por qué un agente aceptó o rechazó."""

    order_id: int
    accepted: bool
    score: float
    estimated_travel_time_sec: float
    estimated_risk: Optional[float] = None
    net_profit_estimate_mxn: Optional[float] = None
    reasoning: str
    timed_out: bool = False
    hard_safety_violation: bool = Field(
        False,
        description=(
            "True cuando el rechazo fue por el límite duro de seguridad "
            "(riesgo de ruta demasiado alto), sin importar el pago "
            "ofrecido -- distinto de un rechazo por rentabilidad. Un "
            "agente que no evalúa riesgo (Greedy) nunca lo marca."
        ),
    )
    exceeds_safety_threshold: Optional[bool] = Field(
        None,
        description=(
            "Informativo: True si el riesgo de esta oferta supera "
            "`risk_model.SAFETY_HARD_RISK_LIMIT`, calculado igual para "
            "TODOS los agentes (incluido uno que, como Greedy, no actúa "
            "sobre esto). Permite medir cuántos pedidos de alto riesgo "
            "acepta un agente ciego al riesgo que el adverso al riesgo sí "
            "rechazaría -- la comparación central del KPI de seguridad."
        ),
    )
    decision_latency_ms: Optional[float] = Field(
        None,
        description=(
            "Tiempo real que tardó la decisión, medido por el motor de "
            "simulación alrededor de `evaluate_offer`. Evidencia en vivo de "
            "que se respeta el límite duro de 200 ms del reto; en un "
            "timeout vale exactamente `decision_timeout_ms`."
        ),
    )


class BaseAgent(ABC):
    """
    Contrato que deben cumplir todos los agentes de decisión del simulador.

    `evaluate_offer` es asíncrono porque, en un despliegue real, puede
    involucrar I/O (consultar el grafo vial compartido, un servicio de
    clima, telemetría de tráfico, etc.). El motor de simulación (`server.py`)
    envuelve cada llamada con `asyncio.wait_for(..., timeout=0.2)`: si el
    agente no responde en 200 ms la oferta se rechaza automáticamente por
    seguridad, sin excepción.

    `plan_route` es intencionalmente síncrono: el ruteo (nearest-neighbor o
    OR-Tools) es CPU-bound y determinista dado un estado; el motor lo invoca
    a través de `asyncio.to_thread` para no bloquear el event loop, tras
    haber precalculado de forma asíncrona cualquier matriz de distancias que
    el método necesite.

    Dos hooks OPCIONALES, detectados por duck-typing en `server.py` (no son
    parte del contrato abstracto porque solo tienen sentido para un agente
    que pondera riesgo):
    - `async def prepare_route_matrix(driver_state, active_orders, shift_state)`:
      precalcula lo que `plan_route` necesite antes de que el motor lo
      invoque en un hilo.
    - `async def risk_weighted_path(origin_node, dest_node, shift_state) -> Tuple[int, ...]`:
      la ruta FÍSICA que el motor dibuja/recorre de verdad. Un agente que no
      lo implemente (p. ej. `GreedyAgent`) siempre recorre el camino más
      rápido a secas, sin importar los eventos activos.
    """

    name: str
    alpha: float

    def __init__(self, name: str, alpha: float) -> None:
        self.name = name
        self.alpha = alpha

    @abstractmethod
    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        """Decide si aceptar o rechazar `offer` dado el estado actual del
        repartidor y las condiciones del turno. Debe completarse en menos
        de 200 ms; el motor de simulación aplicará el timeout duro."""
        raise NotImplementedError

    @abstractmethod
    def plan_route(
        self, driver_state: DriverState, active_orders: List[Offer]
    ) -> List[int]:
        """Devuelve la secuencia de `order_id` en el orden en que deben
        atenderse los pedidos activos del repartidor."""
        raise NotImplementedError
