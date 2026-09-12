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
    timeouts_incurred: int = 0
    active_orders: List[Offer] = Field(default_factory=list)


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
