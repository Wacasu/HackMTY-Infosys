"""
greedy_agent.py  (PASO 3)
==========================
Agente Base del reto "The Courier": alpha = 0.

Filosofía de decisión: maximizar ganancia bruta inmediata por minuto de
trabajo (pago / tiempo de viaje real), ignorando por completo el riesgo
dinámico (lluvia, inundaciones, tráfico). Es el "corredor imprudente" contra
el que se compara el Agente Inteligente.

El único costo que este agente reconoce es Te (tiempo estimado de viaje real
sobre la malla vial de Monterrey, vía `CityGraphProvider`). Nunca consulta
`ShiftState.active_events` para DECIDIR: por diseño, es ciego al riesgo.

Sí MIDE el riesgo de cada viaje (vía `risk_model`, el mismo módulo que usa
el Agente Inteligente) para poder REPORTARLO en el panel de KPIs -- sin esa
medición no habría forma de comparar con datos reales "qué tan riesgosos
son los viajes que un repartidor ciego al riesgo termina aceptando". Esa
medición nunca entra en la decisión de aceptar/rechazar.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from agent_interface import AgentDecision, BaseAgent, DriverState, Offer, ShiftState
from city_graph import CityGraphProvider
from risk_model import SAFETY_HARD_RISK_LIMIT, route_risk_and_time, weighted_route_risk

logger = logging.getLogger("the_courier.greedy_agent")

# Umbral mínimo de pago por minuto de viaje (MXN/min) para aceptar una
# oferta. Por debajo de este umbral el pedido "no vale la pena" incluso para
# un agente que ignora el riesgo.
MIN_ACCEPTABLE_MXN_PER_MINUTE = 3.0

# Piso de tiempo (segundos) usado para evitar divisiones por cero cuando el
# pickup y el dropoff están extremadamente cerca en la malla vial.
MIN_TRAVEL_TIME_FLOOR_SEC = 30.0


class GreedyAgent(BaseAgent):
    """
    Agente Base (alpha = 0): acepta cualquier oferta cuyo pago por minuto de
    viaje real supere `MIN_ACCEPTABLE_MXN_PER_MINUTE`, sin ponderar riesgo.
    """

    def __init__(self, graph_provider: CityGraphProvider) -> None:
        super().__init__(name="greedy_base", alpha=0.0)
        self._graph_provider = graph_provider

    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        # Mismo Dijkstra que ya se necesitaba para el tiempo de viaje (Te);
        # `route_risk_and_time` de paso muestrea el riesgo a lo largo de esa
        # misma ruta, así que medirlo no cuesta un segundo Dijkstra extra.
        (risk_to_pickup, leg_to_pickup_sec), (risk_delivery, leg_to_dropoff_sec) = await asyncio.gather(
            route_risk_and_time(self._graph_provider, driver_state.current_node, offer.pickup_node, shift_state),
            route_risk_and_time(self._graph_provider, offer.pickup_node, offer.dropoff_node, shift_state),
        )
        total_travel_sec = max(
            leg_to_pickup_sec + leg_to_dropoff_sec + offer.service_time_sec,
            MIN_TRAVEL_TIME_FLOOR_SEC,
        )
        weighted_risk = weighted_route_risk(
            risk_to_pickup, leg_to_pickup_sec, risk_delivery, leg_to_dropoff_sec
        )

        # La decisión sigue siendo EXCLUSIVAMENTE sobre pago/minuto: el
        # riesgo recién calculado no participa aquí, a propósito.
        pay_per_minute = offer.base_fare_mxn / (total_travel_sec / 60.0)
        accepted = pay_per_minute >= MIN_ACCEPTABLE_MXN_PER_MINUTE
        reasoning = (
            f"Pago estimado {pay_per_minute:.2f} MXN/min "
            f"({'>=' if accepted else '<'} umbral {MIN_ACCEPTABLE_MXN_PER_MINUTE:.2f}); "
            "riesgo dinámico ignorado por diseño (alpha=0)."
        )

        return AgentDecision(
            order_id=offer.order_id,
            accepted=accepted,
            score=pay_per_minute,
            estimated_travel_time_sec=total_travel_sec,
            estimated_risk=weighted_risk,
            net_profit_estimate_mxn=offer.base_fare_mxn,
            reasoning=reasoning,
            exceeds_safety_threshold=weighted_risk >= SAFETY_HARD_RISK_LIMIT,
        )

    def plan_route(self, driver_state: DriverState, active_orders: List[Offer]) -> List[int]:
        """Heurística FIFO: el agente Greedy no reoptimiza secuencias de
        entrega, simplemente atiende los pedidos activos en el orden en que
        los aceptó (consistente con su naturaleza cortoplacista)."""
        return [order.order_id for order in active_orders]
