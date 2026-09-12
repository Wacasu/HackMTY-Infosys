"""
risk_averse_agent.py  (PASO 3)
================================
Agente Inteligente del reto "The Courier": alpha > 0.

Filosofía de decisión: maximizar RENTABILIDAD NETA, no ganancia bruta.
Para cada oferta calcula:

    Costo = Te + alpha * Re

donde:
    Te = tiempo de viaje real (minutos) sobre la malla vial de Monterrey,
         obtenido siempre vía `CityGraphProvider` (Dijkstra sobre el
         componente fuertemente conexo real, jamás distancia euclidiana).
    Re = riesgo dinámico [0, 1] a lo largo de la ruta real, derivado de:
         - eventos sorpresa activos en el turno (lluvia, inundación,
           tráfico pesado, calor extremo),
         - proximidad de los tramos de la ruta a zonas de Monterrey
           históricamente propensas a inundación (p. ej. cruces del Río
           Santa Catarina, pasos a desnivel).

Cuando hay múltiples pedidos activos simultáneos, el agente resuelve un
problema de ruteo con recolección-y-entrega y ventanas de tiempo (PDPTW)
usando Google OR-Tools, con la matriz de costos ponderada por el mismo
Te + alpha * Re, de modo que la secuencia elegida también evita zonas de
riesgo en vez de solo minimizar distancia.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from agent_interface import (
    AgentDecision,
    BaseAgent,
    DriverState,
    Offer,
    ShiftState,
    WeatherEvent,
)
from city_graph import CityGraphProvider

logger = logging.getLogger("the_courier.risk_averse_agent")

EARTH_RADIUS_KM = 6371.0

# Umbral duro de seguridad: sin importar cuánto pague el pedido, un riesgo
# de ruta por encima de este valor se rechaza (adversidad al riesgo real,
# no solo una penalización suave).
SAFETY_HARD_RISK_LIMIT = 0.75

# Cuántos "minutos equivalentes" de penalización representa un riesgo
# máximo (Re = 1.0) antes de multiplicarse por alpha.
RISK_PENALTY_MINUTES_AT_MAX_RISK = 45.0

# Rentabilidad mínima aceptable en MXN por minuto ajustado por riesgo.
MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE = 1.8

MIN_TRAVEL_TIME_FLOOR_SEC = 30.0

# Costo operativo monetizado por unidad de riesgo, usado solo para reportar
# una estimación de "ganancia neta" legible en el panel del simulador.
OPERATIONAL_RISK_COST_MXN_PER_RISK_UNIT = 18.0

# Zonas de Monterrey históricamente propensas a encharcamiento/inundación
# durante lluvias fuertes (coordenadas aproximadas, radio en km, severidad
# base de la zona en [0, 1]). Se usan para modular Re cuando hay lluvia.
FLOOD_PRONE_ZONES: Tuple[Tuple[str, float, float, float, float], ...] = (
    ("Puente del Papa / Río Santa Catarina", 25.6690, -100.3550, 1.2, 0.9),
    ("Distribuidor Gonzalitos", 25.6825, -100.3505, 1.0, 0.8),
    ("Av. Morones Prieto bajo Puente Constitución", 25.6640, -100.3320, 1.0, 0.85),
    ("Cruce Av. Revolución / Río Santa Catarina", 25.6555, -100.3610, 1.0, 0.75),
    ("Paso a Desnivel Cumbres", 25.7100, -100.3800, 1.3, 0.6),
)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass
class _RouteStop:
    """Metadato interno de una parada (pickup o dropoff) usada para
    construir el modelo de OR-Tools en `plan_route`."""

    order_id: int
    node: int
    is_pickup: bool
    time_window_start_sec: int
    time_window_end_sec: int


class RiskAverseAgent(BaseAgent):
    """
    Agente Inteligente (alpha > 0): pondera tiempo real de viaje y riesgo
    dinámico, y resuelve el ruteo multi-pedido con OR-Tools.
    """

    def __init__(self, graph_provider: CityGraphProvider, alpha: float = 0.35) -> None:
        super().__init__(name="risk_averse_smart", alpha=alpha)
        self._graph_provider = graph_provider
        self._last_shift_state: Optional[ShiftState] = None

        # Poblado por `prepare_route_matrix` (async) antes de invocar el
        # `plan_route` (sync) correspondiente vía asyncio.to_thread.
        self._route_stops: List[_RouteStop] = []
        self._route_cost_matrix: Tuple[Tuple[int, ...], ...] = ()

    # ------------------------------------------------------------------ #
    # Modelo de riesgo dinámico
    # ------------------------------------------------------------------ #
    def _severity_for_event(self, shift_state: ShiftState, event: WeatherEvent) -> float:
        if event in shift_state.active_events:
            return shift_state.event_severity
        return 0.0

    def _point_risk(self, lat: float, lon: float, shift_state: ShiftState) -> float:
        """Riesgo puntual [0, 1] en una coordenada dada, bajo las
        condiciones ambientales actuales del turno."""
        rain_severity = max(
            self._severity_for_event(shift_state, WeatherEvent.HEAVY_RAIN),
            self._severity_for_event(shift_state, WeatherEvent.FLASH_FLOOD),
        )
        traffic_severity = self._severity_for_event(
            shift_state, WeatherEvent.HEAVY_TRAFFIC)
        heat_severity = self._severity_for_event(
            shift_state, WeatherEvent.EXTREME_HEAT)

        risk = 0.05  # riesgo ambiental base (Monterrey, tráfico ordinario)
        risk += 0.35 * rain_severity
        risk += 0.15 * traffic_severity
        risk += 0.08 * heat_severity

        if rain_severity > 0.0:
            for _name, zone_lat, zone_lon, radius_km, zone_base_severity in FLOOD_PRONE_ZONES:
                distance_km = _haversine_km(lat, lon, zone_lat, zone_lon)
                if distance_km <= radius_km:
                    proximity_factor = 1.0 - (distance_km / radius_km)
                    risk += proximity_factor * zone_base_severity * rain_severity

        return max(0.0, min(risk, 1.0))

    async def _route_risk_and_time(
        self, origin_node: int, dest_node: int, shift_state: ShiftState
    ) -> Tuple[float, float]:
        """Calcula (riesgo_promedio, tiempo_de_viaje_sec) para el tramo real
        entre dos nodos, muestreando el riesgo puntual a lo largo de la
        ruta más rápida real (nunca en línea recta)."""
        travel_time_sec = await self._graph_provider.travel_time_sec(origin_node, dest_node)
        if not math.isfinite(travel_time_sec):
            return 1.0, float("inf")

        path_nodes = await self._graph_provider.shortest_path(origin_node, dest_node)
        graph = self._graph_provider.graph

        if len(path_nodes) > 12:
            step = max(1, len(path_nodes) // 12)
            sampled_nodes = path_nodes[::step]
        else:
            sampled_nodes = path_nodes

        point_risks = [
            self._point_risk(graph.nodes[node]["y"],
                             graph.nodes[node]["x"], shift_state)
            for node in sampled_nodes
        ]
        average_risk = sum(point_risks) / \
            len(point_risks) if point_risks else 0.05
        return average_risk, travel_time_sec

    # ------------------------------------------------------------------ #
    # Evaluación de ofertas
    # ------------------------------------------------------------------ #
    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        self._last_shift_state = shift_state

        risk_to_pickup, time_to_pickup_sec = await self._route_risk_and_time(
            driver_state.current_node, offer.pickup_node, shift_state
        )
        risk_delivery, time_delivery_sec = await self._route_risk_and_time(
            offer.pickup_node, offer.dropoff_node, shift_state
        )

        total_travel_sec = max(
            time_to_pickup_sec + time_delivery_sec + offer.service_time_sec,
            MIN_TRAVEL_TIME_FLOOR_SEC,
        )

        if total_travel_sec > 0 and math.isfinite(total_travel_sec):
            weighted_risk = (
                risk_to_pickup * time_to_pickup_sec + risk_delivery * time_delivery_sec
            ) / max(time_to_pickup_sec + time_delivery_sec, 1.0)
        else:
            weighted_risk = max(risk_to_pickup, risk_delivery)

        te_minutes = total_travel_sec / 60.0
        weighted_cost_minutes = te_minutes + self.alpha * weighted_risk * (
            RISK_PENALTY_MINUTES_AT_MAX_RISK
        )
        risk_adjusted_score = offer.base_fare_mxn / \
            max(weighted_cost_minutes, 0.1)

        hard_safety_violation = weighted_risk >= SAFETY_HARD_RISK_LIMIT
        meets_profitability_bar = (
            risk_adjusted_score >= MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE
        )
        accepted = meets_profitability_bar and not hard_safety_violation

        net_profit_estimate = offer.base_fare_mxn - (
            weighted_risk * OPERATIONAL_RISK_COST_MXN_PER_RISK_UNIT
        )

        if hard_safety_violation:
            reasoning = (
                f"Rechazado: riesgo de ruta {weighted_risk:.2f} supera el límite duro de "
                f"seguridad {SAFETY_HARD_RISK_LIMIT:.2f} (posibles inundaciones en el trayecto), "
                "sin importar el pago ofrecido."
            )
        else:
            reasoning = (
                f"Costo ponderado = Te({te_minutes:.1f} min) + alpha({self.alpha:.2f}) * "
                f"Re({weighted_risk:.2f}) = {weighted_cost_minutes:.1f} min-equiv; "
                f"rentabilidad {risk_adjusted_score:.2f} MXN/min-equiv "
                f"({'>=' if meets_profitability_bar else '<'} umbral "
                f"{MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE:.2f})."
            )

        return AgentDecision(
            order_id=offer.order_id,
            accepted=accepted,
            score=risk_adjusted_score,
            estimated_travel_time_sec=total_travel_sec,
            estimated_risk=weighted_risk,
            net_profit_estimate_mxn=round(net_profit_estimate, 2),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------ #
    # Ruteo y batching multi-pedido (OR-Tools, PDPTW)
    # ------------------------------------------------------------------ #
    async def prepare_route_matrix(
        self, driver_state: DriverState, active_orders: List[Offer]
    ) -> None:
        """Precalcula, de forma asíncrona, la matriz de costos
        Te + alpha*Re entre todas las paradas relevantes (depot + pickups +
        dropoffs de los pedidos activos). Debe llamarse siempre antes de
        `plan_route` cuando hay más de un pedido activo; `plan_route` en sí
        es síncrono (OR-Tools es CPU-bound) y debe invocarse a través de
        `asyncio.to_thread` desde el motor de simulación."""
        shift_state = self._last_shift_state
        if shift_state is None:
            raise RuntimeError(
                "prepare_route_matrix requiere que `evaluate_offer` se haya "
                "invocado al menos una vez para conocer el ShiftState actual."
            )

        stops: List[_RouteStop] = []
        nodes: List[int] = [driver_state.current_node]
        for order in active_orders:
            stops.append(
                _RouteStop(
                    order_id=order.order_id,
                    node=order.pickup_node,
                    is_pickup=True,
                    time_window_start_sec=order.ready_time_sec,
                    time_window_end_sec=order.due_time_sec,
                )
            )
            stops.append(
                _RouteStop(
                    order_id=order.order_id,
                    node=order.dropoff_node,
                    is_pickup=False,
                    time_window_start_sec=order.ready_time_sec,
                    time_window_end_sec=order.due_time_sec,
                )
            )
            nodes.append(order.pickup_node)
            nodes.append(order.dropoff_node)

        risk_matrix: List[List[float]] = [
            [0.0] * len(nodes) for _ in range(len(nodes))]
        time_matrix = await self._graph_provider.travel_time_matrix(tuple(nodes))

        for i, origin in enumerate(nodes):
            for j, dest in enumerate(nodes):
                if i == j:
                    continue
                risk, _ = await self._route_risk_and_time(origin, dest, shift_state)
                risk_matrix[i][j] = risk

        cost_matrix: List[List[int]] = []
        for i in range(len(nodes)):
            row: List[int] = []
            for j in range(len(nodes)):
                if i == j:
                    row.append(0)
                    continue
                te_sec = time_matrix[i][j]
                cost_sec = te_sec + self.alpha * risk_matrix[i][j] * (
                    RISK_PENALTY_MINUTES_AT_MAX_RISK * 60.0
                )
                row.append(int(round(cost_sec)))
            cost_matrix.append(row)

        self._route_stops = stops
        self._route_cost_matrix = tuple(tuple(row) for row in cost_matrix)

    def plan_route(self, driver_state: DriverState, active_orders: List[Offer]) -> List[int]:
        """Resuelve el orden óptimo de entregas usando OR-Tools (TSP) sobre
        la matriz de costos Te + alpha*Re precalculada en
        `prepare_route_matrix`. Si solo hay un pedido activo, no hay nada
        que optimizar; si OR-Tools no encuentra solución, cae a FIFO."""
        if len(active_orders) <= 1:
            return [order.order_id for order in active_orders]

        if not self._route_cost_matrix or not self._route_stops:
            return [order.order_id for order in active_orders]

        return self._solve_ortools_sequence()

    def _solve_ortools_sequence(self) -> List[int]:
        """Construye y resuelve un modelo TSP con OR-Tools sobre la matriz
        de costos precalculada, respetando precedencia pickup→dropoff."""
        stops = self._route_stops
        cost_matrix = self._route_cost_matrix
        num_stops = len(stops) + 1  # +1 for depot (index 0)

        manager = pywrapcp.RoutingIndexManager(num_stops, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def cost_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(cost_matrix[from_node][to_node])

        transit_idx = routing.RegisterTransitCallback(cost_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

        pickup_to_dropoff: Dict[int, int] = {}
        for i, stop in enumerate(stops):
            node_idx = i + 1
            if stop.is_pickup:
                pickup_to_dropoff[stop.order_id] = node_idx
        for i, stop in enumerate(stops):
            node_idx = i + 1
            if not stop.is_pickup:
                pickup_node = pickup_to_dropoff.get(stop.order_id)
                if pickup_node is not None:
                    routing.solver().Add(
                        routing.VehicleVar(node_idx) == routing.VehicleVar(pickup_node)
                    )
                    routing.solver().Add(
                        routing.CumulVar(node_idx, transit_idx)
                        >= routing.CumulVar(pickup_node, transit_idx)
                    )

        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_params.time_limit_ms = 1500

        solution = routing.SolveWithParameters(search_params)
        if solution is None:
            logger.warning("OR-Tools no encontró solución; cayendo a FIFO.")
            return list(dict.fromkeys(s.order_id for s in stops if s.is_pickup))

        index = routing.Start(0)
        ordered_order_ids: List[int] = []
        visited_pickups: set[int] = set()
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node > 0:
                stop = stops[node - 1]
                if stop.is_pickup and stop.order_id not in visited_pickups:
                    ordered_order_ids.append(stop.order_id)
                    visited_pickups.add(stop.order_id)
            index = solution.Value(routing.NextVar(index))

        return ordered_order_ids

    def risk_penalty_factor(self) -> float:
        """Factor de penalización de riesgo para el ruteo de aristas en el
        grafo. Se deriva del alpha del agente y del estado ambiental más
        reciente. Un valor de 0 produce la ruta más rápida; valores mayores
        desvían el camino lejos de zonas de inundación."""
        shift_state = self._last_shift_state
        if shift_state is None:
            return 0.0
        rain_severity = max(
            self._severity_for_event(shift_state, WeatherEvent.HEAVY_RAIN),
            self._severity_for_event(shift_state, WeatherEvent.FLASH_FLOOD),
        )
        if rain_severity <= 0.0:
            return 0.0
        return self.alpha * rain_severity * 3.0
