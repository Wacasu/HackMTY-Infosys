"""
greedy_agent.py  (PASO 3)
==========================
Agente Base del reto "The Courier": alpha = 0.

Filosofía: acepta TODO (aceptación obligatoria, igual que Risk-Averse) y
rutea por el camino más rápido a secas, ignorando por completo el riesgo
dinámico (lluvia, inundaciones, tráfico) tanto al planear la ruta como al
puntuar la oferta. Es el "corredor imprudente" contra el que se compara el
Agente Inteligente -- ya no porque rechace menos, sino porque su ruta real
expone al repartidor a más riesgo y su secuenciación (FIFO simple) no
prioriza por densidad de puntos dentro del tiempo del turno.

El único costo que este agente reconoce es Te (tiempo estimado de viaje real
sobre la malla vial de Monterrey, vía `CityGraphProvider`). Nunca consulta
`ShiftState.active_events` para rutear: por diseño, es ciego al riesgo.

Sí MIDE el riesgo de cada viaje (vía `risk_model`, el mismo módulo que usa
el Agente Inteligente) para poder REPORTARLO en el panel de KPIs y calcular
su Bonus de Riesgo -- sin esa medición no habría forma de comparar con datos
reales qué tan riesgosos son los viajes que un repartidor ciego al riesgo
termina recorriendo.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from agent_interface import AgentDecision, BaseAgent, DriverState, Offer, ShiftState
from city_graph import CityGraphProvider
from risk_model import (
    SAFETY_HARD_RISK_LIMIT,
    candidate_pickup_origins,
    risk_bonus_points,
    route_risk_and_time,
    weighted_route_risk,
)

logger = logging.getLogger("the_courier.greedy_agent")

# Piso de tiempo (segundos) usado para evitar divisiones por cero cuando el
# pickup y el dropoff están extremadamente cerca en la malla vial.
MIN_TRAVEL_TIME_FLOOR_SEC = 30.0


class GreedyAgent(BaseAgent):
    """
    Agente Base (alpha = 0): aceptación obligatoria, ruta siempre por el
    camino más rápido a secas, sin ponderar riesgo en ningún momento.
    """

    def __init__(self, graph_provider: CityGraphProvider) -> None:
        super().__init__(name="greedy_base", alpha=0.0)
        self._graph_provider = graph_provider

    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        # Aceptación obligatoria: no hay umbral de pago ni de riesgo que
        # pueda rechazar esta oferta. Lo que sigue calculando (Te, riesgo)
        # es puramente informativo -- alimenta el score, el Bonus de Riesgo
        # estimado y el panel de KPIs, nunca una decisión de aceptar/rechazar.
        #
        # La pierna de recogida se evalúa desde varios orígenes candidatos
        # (posición actual + dropoffs de pedidos ya en cola) y se usa el
        # más barato: si este pickup cae cerca de un dropoff que el
        # repartidor ya trae en curso, así se refleja en el costo real en
        # vez de medirse siempre desde donde está parado ahora mismo.
        candidate_origins = candidate_pickup_origins(driver_state, offer)
        pickup_leg_results, (risk_delivery, leg_to_dropoff_sec) = await asyncio.gather(
            asyncio.gather(*(
                route_risk_and_time(self._graph_provider, origin, offer.pickup_node, shift_state)
                for origin in candidate_origins
            )),
            route_risk_and_time(self._graph_provider, offer.pickup_node, offer.dropoff_node, shift_state),
        )
        risk_to_pickup, leg_to_pickup_sec = min(pickup_leg_results, key=lambda pair: pair[1])
        total_travel_sec = max(
            leg_to_pickup_sec + leg_to_dropoff_sec + offer.service_time_sec,
            MIN_TRAVEL_TIME_FLOOR_SEC,
        )
        weighted_risk = weighted_route_risk(
            risk_to_pickup, leg_to_pickup_sec, risk_delivery, leg_to_dropoff_sec
        )
        points_per_minute = offer.base_points / (total_travel_sec / 60.0)
        estimated_bonus = risk_bonus_points(weighted_risk)

        reasoning = (
            f"Accepted (mandatory acceptance): {points_per_minute:.2f} pts/min, "
            f"route risk {weighted_risk:.2f} ignored by design (alpha=0) -- "
            f"estimated risk bonus {estimated_bonus:.1f} pts."
        )

        return AgentDecision(
            order_id=offer.order_id,
            score=points_per_minute,
            estimated_travel_time_sec=total_travel_sec,
            estimated_risk=weighted_risk,
            points_bonus_estimate=estimated_bonus,
            reasoning=reasoning,
            exceeds_safety_threshold=weighted_risk >= SAFETY_HARD_RISK_LIMIT,
        )

    def plan_route(self, driver_state: DriverState, active_orders: List[Offer]) -> List[int]:
        """Heurística FIFO: el agente Greedy no reoptimiza secuencias de
        entrega, simplemente atiende los pedidos activos en el orden en que
        entraron a la cola (consistente con su naturaleza cortoplacista --
        no prioriza por densidad de puntos ni por lo que alcanza a cumplir
        dentro del turno, a diferencia del ruteo orientado a puntos de
        Risk-Averse)."""
        return [order.order_id for order in active_orders]
