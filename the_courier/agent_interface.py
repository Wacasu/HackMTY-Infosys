"""
agent_interface.py
===================
Modelos de dominio compartidos y contrato abstracto (`BaseAgent`) que deben
implementar tanto el Agente Base (Greedy) como el Agente Inteligente
(Risk-Averse) del simulador "The Courier".

Todos los modelos de datos usan Pydantic para poder serializarse
directamente a JSON sobre el WebSocket de `server.py` sin capas de
traducción adicionales.

Esquema de recompensas CERRADO (puntos, no dinero)
---------------------------------------------------
Ambos agentes tienen ACEPTACIÓN OBLIGATORIA: `evaluate_offer` ya no puede
rechazar una oferta por rentabilidad ni por riesgo -- toda oferta entra a
la cola de ruteo (`DriverState.active_orders`) sin excepción. La
diferencia entre Greedy y Risk-Averse ya no está en QUÉ aceptan (los dos
aceptan el 100%), sino en:
  1. Qué tan bien RUTEAN dentro del tiempo del turno (`plan_route`
     orientado a maximizar puntos, no solo a minimizar distancia).
  2. Qué tan seguro es el CAMINO FÍSICO que de verdad recorren
     (`risk_weighted_path`), lo que cambia cuánto `points_risk_bonus`
     gana cada uno por el mismo pedido.
  3. Cuántos pedidos quedan sin entregar cuando se acaba el turno
     (`points_lost_timeout`) -- con aceptación obligatoria y sin ningún
     filtro de admisión, es esperable que la cola crezca más rápido de lo
     que un solo repartidor puede vaciar, y un ruteo que prioriza mal
     paga ese costo en puntos nunca cobrados, no en pedidos rechazados.
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
    base_points: float = Field(
        ..., gt=0, description="Puntos base que otorga completar este pedido -- "
        "reemplaza el pago monetario del esquema anterior."
    )
    straight_line_distance_km: float = Field(
        ..., description="Solo referencia informativa; NUNCA usar para decidir rutas"
    )

    class Config:
        frozen = True


class DriverState(BaseModel):
    """Estado mutable del repartidor dentro de un motor de simulación."""

    current_node: int
    current_time_sec: int
    distance_traveled_km: float = 0.0
    orders_completed: int = Field(0, description="Pedidos Entregados.")
    orders_delivered_late: int = Field(
        0, description="De `orders_completed`, cuántos se entregaron después "
        "de su `due_time_sec` -- se aceptaron dentro de su ventana, pero para "
        "cuando les tocó turno en la cola del repartidor ya había vencido.")
    timeouts_incurred: int = Field(
        0, description="Decisiones que tardaron más de 200 ms -- ya no "
        "implica rechazo (la aceptación es obligatoria), solo mide qué tan "
        "seguido un agente raspa el límite duro de tiempo de decisión.")
    active_orders: List[Offer] = Field(default_factory=list)

    # --- Sistema de puntos (desglose) ------------------------------------
    points_base: float = Field(
        0.0, description="Puntos Base Obtenidos: suma de `Offer.base_points` "
        "de cada pedido ENTREGADO (no de los aceptados -- un pedido huérfano "
        "que nunca se entrega no aporta aquí, ver `points_lost_timeout`).")
    points_risk_bonus: float = Field(
        0.0, description="Bonus de Riesgo: puntos extra por cada entrega, "
        "proporcionales a qué tan bajo fue el riesgo de la ruta REAL que el "
        "repartidor recorrió para cumplirla (ver risk_model.risk_bonus_points). "
        "Con aceptación obligatoria para los dos agentes, este bonus -- no un "
        "rechazo -- es lo que recompensa rutear de forma más segura.")
    points_lost_timeout: float = Field(
        0.0, description="Puntos no obtenidos por falta de tiempo: al cerrar "
        "el turno, la suma de `base_points` de los pedidos que seguían en "
        "`active_orders` sin entregarse -- el costo de oportunidad de la cola "
        "que la aceptación obligatoria dejó sin vaciar a tiempo. Se calcula "
        "UNA sola vez, al final, con aritmética simple sobre datos ya en "
        "memoria (sin I/O ni Dijkstra) para que el cierre del turno sea "
        "determinista y no bloquee el event loop.")

    # --- KPI de seguridad (medido, no decidido) --------------------------
    # Con aceptación obligatoria para ambos agentes, el riesgo ya no se usa
    # para rechazar -- se sigue MIDIENDO sobre la ruta real que cada quien
    # recorre, porque esa ruta sí puede diferir (Greedy va por el camino más
    # rápido a secas; Risk-Averse por el camino Te+alpha*Re). Es lo que
    # sigue justificando el modelo: mismos pedidos, mismo 100% de entregas
    # eventuales, pero exposición al riesgo distinta.
    total_risk_sum: float = Field(
        0.0, description="Suma de `estimated_risk` de cada pedido entregado.")
    total_risk_count: int = Field(
        0, description="Cuántos pedidos entregados incluyeron `estimated_risk` "
        "(promedio = total_risk_sum / total_risk_count).")
    risky_orders_handled: int = Field(
        0, description="De los pedidos entregados, cuántos tuvieron una ruta "
        "real con riesgo >= `risk_model.SAFETY_HARD_RISK_LIMIT` -- ya no es "
        "un conteo de \"aceptados a pesar del riesgo\" (todo se acepta), sino "
        "de cuántas veces el camino que el repartidor de verdad recorrió "
        "pasó por una exposición objetivamente alta.")


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
    """Resultado estructurado de `evaluate_offer`. Con aceptación
    obligatoria, `accepted` es siempre `True` -- se conserva el campo (en
    vez de eliminarlo) para no romper la forma del mensaje que ya consume
    el frontend, y porque `timed_out` sigue siendo información real aunque
    ya no cambie el resultado de la decisión."""

    order_id: int
    accepted: bool = True
    score: float
    estimated_travel_time_sec: float
    estimated_risk: Optional[float] = None
    points_bonus_estimate: Optional[float] = Field(
        None, description="Estimación del Bonus de Riesgo que ganaría este "
        "pedido si se entrega con el riesgo medido aquí -- informativo, el "
        "monto real se recalcula sobre la ruta que de verdad se recorrió al "
        "completarlo (ver ShiftSimulatorEngine._complete_current_order)."
    )
    reasoning: str
    timed_out: bool = False
    exceeds_safety_threshold: Optional[bool] = Field(
        None,
        description=(
            "Informativo: True si el riesgo de esta oferta supera "
            "`risk_model.SAFETY_HARD_RISK_LIMIT`, calculado igual para "
            "TODOS los agentes. Ya no causa rechazo (aceptación obligatoria) "
            "-- solo alimenta `risky_orders_handled`."
        ),
    )
    decision_latency_ms: Optional[float] = Field(
        None,
        description=(
            "Tiempo real que tardó la decisión, medido por el motor de "
            "simulación alrededor de `evaluate_offer`. Con aceptación "
            "obligatoria ya no arriesga un rechazo, pero se sigue "
            "reportando como evidencia de que la decisión (aceptar +"
            " calcular puntos/riesgo) cabe en el límite duro de 200 ms."
        ),
    )


class BaseAgent(ABC):
    """
    Contrato que deben cumplir todos los agentes de decisión del simulador.

    `evaluate_offer` es asíncrono porque, en un despliegue real, puede
    involucrar I/O (consultar el grafo vial compartido, un servicio de
    clima, telemetría de tráfico, etc.). El motor de simulación (`server.py`)
    sigue envolviendo cada llamada con `asyncio.wait_for(..., timeout=0.2)`
    por disciplina arquitectónica (ningún cálculo de decisión debe poder
    colgar el event loop), pero un timeout ya NO rechaza la oferta -- con
    aceptación obligatoria, el motor la agrega a la cola de todos modos y
    solo registra que la decisión fue lenta.

    `plan_route` es intencionalmente síncrono: el ruteo (nearest-neighbor o
    OR-Tools) es CPU-bound y determinista dado un estado; el motor lo invoca
    a través de `asyncio.to_thread` para no bloquear el event loop, tras
    haber precalculado de forma asíncrona cualquier matriz de distancias que
    el método necesite. Con la cola potencialmente más larga de lo que cabe
    en el turno (aceptación obligatoria, sin filtro de admisión), un
    `plan_route` orientado a puntos puede legítimamente devolver MENOS
    `order_id` que `len(active_orders)` -- los que no entraron en la
    ventana de tiempo simplemente se reconsideran en la siguiente
    replanificación, o terminan en `points_lost_timeout` si el turno se
    acaba antes.

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
        """Acepta `offer` incondicionalmente (aceptación obligatoria) y
        calcula los datos de riesgo/puntos que la acompañan. Debe
        completarse en menos de 200 ms; el motor de simulación aplica el
        timeout duro solo como red de seguridad arquitectónica, no como
        mecanismo de rechazo."""
        raise NotImplementedError

    @abstractmethod
    def plan_route(
        self, driver_state: DriverState, active_orders: List[Offer]
    ) -> List[int]:
        """Devuelve la secuencia de `order_id` a atender, priorizando
        maximizar los puntos cobrables dentro del tiempo restante del
        turno -- no necesariamente todos los `active_orders` caben."""
        raise NotImplementedError
