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

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from agent_interface import (
    AgentDecision,
    BaseAgent,
    DriverState,
    Offer,
    ShiftState,
)
from city_graph import CityGraphProvider
from risk_model import SAFETY_HARD_RISK_LIMIT
from risk_model import point_risk as _shared_point_risk
from risk_model import route_risk_and_time as _shared_route_risk_and_time
from risk_model import weighted_route_risk

logger = logging.getLogger("the_courier.risk_averse_agent")

# Cuántos "minutos equivalentes" de penalización representa un riesgo
# máximo (Re = 1.0) antes de multiplicarse por alpha.
RISK_PENALTY_MINUTES_AT_MAX_RISK = 45.0

# Rentabilidad mínima aceptable en MXN por minuto ajustado por riesgo.
MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE = 2.5

MIN_TRAVEL_TIME_FLOOR_SEC = 30.0

# Costo operativo monetizado por unidad de riesgo, usado solo para reportar
# una estimación de "ganancia neta" legible en el panel del simulador.
OPERATIONAL_RISK_COST_MXN_PER_RISK_UNIT = 18.0


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

        # Cache de riesgo por nodo para `risk_weighted_path` (ver ahí el
        # porqué). Se invalida solo cuando cambian los eventos/severidad.
        self._node_risk_cache: dict[int, float] = {}
        self._node_risk_cache_key: Optional[Tuple[Tuple[str, ...], float]] = None

    # ------------------------------------------------------------------ #
    # Modelo de riesgo dinámico (compartido con `greedy_agent.py` vía
    # `risk_model.py`, para que el KPI de "riesgo promedio aceptado" sea
    # una comparación justa entre los dos agentes -- ver ahí el porqué).
    # ------------------------------------------------------------------ #
    def _point_risk(self, lat: float, lon: float, shift_state: ShiftState) -> float:
<<<<<<< Updated upstream
        """Riesgo puntual [0, 1] en una coordenada dada, bajo las
        condiciones ambientales actuales del turno."""
        rain_severity = max(
            self._severity_for_event(shift_state, WeatherEvent.HEAVY_RAIN),
            self._severity_for_event(shift_state, WeatherEvent.FLASH_FLOOD),
        )
        traffic_severity = self._severity_for_event(shift_state, WeatherEvent.HEAVY_TRAFFIC)
        heat_severity = self._severity_for_event(shift_state, WeatherEvent.EXTREME_HEAT)

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
=======
        return _shared_point_risk(lat, lon, shift_state)
>>>>>>> Stashed changes

    async def _route_risk_and_time(
        self, origin_node: int, dest_node: int, shift_state: ShiftState
    ) -> Tuple[float, float]:
        return await _shared_route_risk_and_time(
            self._graph_provider, origin_node, dest_node, shift_state
        )

    def _node_risk_map(self, shift_state: ShiftState) -> dict[int, float]:
        """Riesgo puntual de CADA nodo del grafo, cacheado por combinación
        de eventos activos + severidad.

        `risk_weighted_path` corre Dijkstra con una función de costo en
        Python (no un peso numérico plano), así que NetworkX la invoca en
        cada arista que examina -- potencialmente miles de veces por
        consulta. Si esa función recalculara `_point_risk` desde cero cada
        vez (como hacía antes), incluye un for sobre las 5 zonas de
        inundación con trigonometría (haversine); medido sobre el grafo
        real: hasta ~700ms por consulta con lluvia intensa activa, sin
        ningún timeout que lo cubra, lo que se sentía como que el motor
        "se trababa" de la nada. Precalcular el riesgo de los ~8 mil nodos
        UNA vez por cambio de clima (no por arista) convierte cada
        evaluación en un lookup de diccionario."""
        cache_key = (
            tuple(sorted(event.value for event in shift_state.active_events)),
            round(shift_state.event_severity, 4),
        )
        if cache_key == self._node_risk_cache_key:
            return self._node_risk_cache

        graph = self._graph_provider.graph
        self._node_risk_cache = {
            node: self._point_risk(data["y"], data["x"], shift_state)
            for node, data in graph.nodes(data=True)
        }
        self._node_risk_cache_key = cache_key
        return self._node_risk_cache

    async def risk_weighted_path(
        self, origin_node: int, dest_node: int, shift_state: ShiftState
    ) -> Tuple[int, ...]:
        """Ruta FÍSICA (secuencia de nodos) que minimiza Te + alpha*Re por
        arista, no solo Te. Esta es la ruta que el motor de simulación
        dibuja y recorre de verdad: si hay un evento activo que eleva Re
        cerca de una zona (p. ej. lluvia junto al Río Santa Catarina), la
        ruta elegida se desvía de esa zona en vez de solo penalizar la
        oferta en la decisión de aceptar/rechazar. `city_graph.py` la
        detecta por duck-typing (`getattr(agent, "risk_weighted_path", None)`)
        para decidir si el Greedy (ciego al riesgo) sigue con la ruta más
        rápida a secas."""
        if self.alpha <= 0.0:
            return await self._graph_provider.shortest_path(origin_node, dest_node)

<<<<<<< Updated upstream
        point_risks = [
            self._point_risk(graph.nodes[node]["y"], graph.nodes[node]["x"], shift_state)
            for node in sampled_nodes
        ]
        average_risk = sum(point_risks) / len(point_risks) if point_risks else 0.05
        return average_risk, travel_time_sec
=======
        node_risk = self._node_risk_map(shift_state)
        alpha = self.alpha
        penalty_sec = RISK_PENALTY_MINUTES_AT_MAX_RISK * 60.0

        def edge_weight(u: int, v: int, edge_datas: dict) -> float:
            best = min(
                edge_datas.values(),
                key=lambda data: float(data.get("travel_time_sec", float("inf"))),
            )
            travel_time_sec = float(best.get("travel_time_sec", float("inf")))
            if not math.isfinite(travel_time_sec):
                return float("inf")
            edge_risk = (node_risk.get(u, 0.05) + node_risk.get(v, 0.05)) / 2.0
            return travel_time_sec + alpha * edge_risk * penalty_sec

        return await self._graph_provider.shortest_path_weighted(
            origin_node, dest_node, edge_weight
        )
>>>>>>> Stashed changes

    # ------------------------------------------------------------------ #
    # Evaluación de ofertas
    # ------------------------------------------------------------------ #
    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        self._last_shift_state = shift_state

        # Las dos piernas (a recoger, y de recoger a entregar) son
        # independientes entre sí: evaluarlas en paralelo en vez de en fila
        # es la diferencia entre ~2x y ~1x el costo de un solo Dijkstra
        # dentro del presupuesto de 200 ms por decisión.
        (risk_to_pickup, time_to_pickup_sec), (risk_delivery, time_delivery_sec) = await asyncio.gather(
            self._route_risk_and_time(driver_state.current_node, offer.pickup_node, shift_state),
            self._route_risk_and_time(offer.pickup_node, offer.dropoff_node, shift_state),
        )

        total_travel_sec = max(
            time_to_pickup_sec + time_delivery_sec + offer.service_time_sec,
            MIN_TRAVEL_TIME_FLOOR_SEC,
        )
        weighted_risk = weighted_route_risk(
            risk_to_pickup, time_to_pickup_sec, risk_delivery, time_delivery_sec
        )

        te_minutes = total_travel_sec / 60.0
        weighted_cost_minutes = te_minutes + self.alpha * weighted_risk * (
            RISK_PENALTY_MINUTES_AT_MAX_RISK
        )
        risk_adjusted_score = offer.base_fare_mxn / max(weighted_cost_minutes, 0.1)

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
            hard_safety_violation=hard_safety_violation,
            exceeds_safety_threshold=hard_safety_violation,
        )

    # ------------------------------------------------------------------ #
    # Ruteo y batching multi-pedido (OR-Tools, PDPTW)
    # ------------------------------------------------------------------ #
    async def prepare_route_matrix(
        self,
        driver_state: DriverState,
        active_orders: List[Offer],
        shift_state: Optional[ShiftState] = None,
    ) -> None:
        """Precalcula, de forma asíncrona, la matriz de costos
        Te + alpha*Re entre todas las paradas relevantes (depot + pickups +
        dropoffs de los pedidos activos). Debe llamarse siempre antes de
        `plan_route` cuando hay más de un pedido activo; `plan_route` en sí
        es síncrono (OR-Tools es CPU-bound) y debe invocarse a través de
        `asyncio.to_thread` desde el motor de simulación."""
        if shift_state is None:
            shift_state = self._last_shift_state
        if shift_state is None:
            raise RuntimeError(
                "prepare_route_matrix requiere un ShiftState (pásalo explícito "
                "o invoca `evaluate_offer` al menos una vez antes)."
            )
        self._last_shift_state = shift_state

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

        risk_matrix: List[List[float]] = [[0.0] * len(nodes) for _ in range(len(nodes))]
        time_matrix = await self._graph_provider.travel_time_matrix(tuple(nodes))

        # Todas las parejas (i, j) se resuelven en paralelo: en secuencia,
        # cada una despacha su propio `shortest_path` a un hilo y un
        # `await` en fila puede estancar el tick de este motor frente al
        # del agente Greedy (que no paga este costo), rompiendo el
        # emparejamiento tick-a-tick del split-screen en `server.py`.
        pairs = [
            (i, j, origin, dest)
            for i, origin in enumerate(nodes)
            for j, dest in enumerate(nodes)
            if i != j
        ]
        risk_results = await asyncio.gather(
            *(self._route_risk_and_time(origin, dest, shift_state) for _, _, origin, dest in pairs)
        )
        for (i, j, _, _), (risk, _) in zip(pairs, risk_results):
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
<<<<<<< Updated upstream
        """Resuelve un PDPTW de un solo vehículo con Google OR-Tools sobre
        la matriz de costos Te + alpha*Re precalculada por
        `prepare_route_matrix`. Devuelve la secuencia de `order_id` en el
        orden en que deben entregarse (cada order_id aparece una sola vez,
        en el momento de su dropoff)."""
        if not active_orders:
            return []

        if not self._route_cost_matrix or len(self._route_stops) != 2 * len(active_orders):
            raise RuntimeError(
                "plan_route requiere una matriz de costos vigente: llama a "
                "`await prepare_route_matrix(driver_state, active_orders)` primero."
            )

        num_locations = len(self._route_stops) + 1  # +1 por el depot (índice 0)
        manager = pywrapcp.RoutingIndexManager(num_locations, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def cost_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return self._route_cost_matrix[from_node][to_node]

        transit_callback_index = routing.RegisterTransitCallback(cost_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        horizon_sec = max(
            (stop.time_window_end_sec for stop in self._route_stops), default=3600
        ) + 3600
        routing.AddDimension(
            transit_callback_index,
            horizon_sec,
            horizon_sec,
            False,
=======
        """Orden en que se atienden los pedidos activos.

        Con 0 o 1 pedido no hay nada que decidir. Con 2+ resuelve un
        pickup-and-delivery con ventanas de tiempo (PDPTW) sobre la matriz
        de costo Te + alpha*Re calculada por `prepare_route_matrix`, usando
        Google OR-Tools cuando el problema lo amerita (3+ pedidos activos) y
        un vecino-más-cercano sobre esa misma matriz para el caso simple de
        2 pedidos o si OR-Tools no encuentra solución factible (deadlines
        demasiado ajustados). El motor de simulación solo ejecuta el primer
        pedido de la secuencia devuelta y vuelve a llamar a este método al
        completarlo, así que cada replanificación ya puede reaccionar a
        pedidos nuevos y a eventos de clima inyectados desde la última vez.
        """
        if not active_orders:
            return []
        if len(active_orders) == 1 or not self._route_cost_matrix or not self._route_stops:
            return [order.order_id for order in active_orders]

        if len(active_orders) >= 3:
            try:
                return self._solve_pdptw(driver_state, active_orders)
            except Exception as exc:
                # Camino esperado y manejado (no un bug): con ventanas de
                # tiempo ajustadas, sobre todo en turnos cortos de prueba,
                # OR-Tools puede no encontrar una solución factible. Se
                # registra en INFO sin traceback -- un ERROR con stack trace
                # aquí se ve como una caída real en la consola del servidor
                # durante una demo, cuando en realidad el respaldo de abajo
                # ya lo resuelve sin interrumpir la sesión.
                logger.info(
                    "OR-Tools no encontro solucion factible para el PDPTW (%s); "
                    "usando vecino mas cercano sobre la matriz de costo "
                    "ponderada por riesgo como respaldo.",
                    exc,
                )

        return self._nearest_neighbor_sequence(active_orders)

    def _nearest_neighbor_sequence(self, active_orders: List[Offer]) -> List[int]:
        """Recorre, de forma golosa, el pedido cuyo *pickup* es más barato
        (Te + alpha*Re) desde la posición actual, encadenando desde el
        *dropoff* del pedido recién elegido. A diferencia del FIFO anterior,
        esto sí usa el riesgo real de la ruta para decidir cuál pedido
        conviene atender primero."""
        order_index = {order.order_id: i for i, order in enumerate(active_orders)}
        remaining = set(order_index)
        sequence: List[int] = []
        current_index = 0  # nodo 0 de la matriz = posición actual del repartidor
        while remaining:
            best_order_id = min(
                remaining,
                key=lambda oid: self._route_cost_matrix[current_index][1 + 2 * order_index[oid]],
            )
            sequence.append(best_order_id)
            current_index = 2 + 2 * order_index[best_order_id]
            remaining.discard(best_order_id)
        return sequence

    def _solve_pdptw(self, driver_state: DriverState, active_orders: List[Offer]) -> List[int]:
        cost_matrix = self._route_cost_matrix
        num_nodes = len(cost_matrix)
        manager = pywrapcp.RoutingIndexManager(num_nodes, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            if to_node == 0:
                # No se exige volver a la posición actual: es un punto de
                # partida, no un depósito al que haya que regresar.
                return 0
            return cost_matrix[from_node][to_node]

        transit_callback_index = routing.RegisterTransitCallback(transit_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        routing.AddDimension(
            transit_callback_index,
            int(RISK_PENALTY_MINUTES_AT_MAX_RISK * 60.0 * 4),  # holgura (slack)
            int(24 * 3600),  # tope acumulado por vehículo
            True,  # el acumulado empieza en 0 en la posición actual
>>>>>>> Stashed changes
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")

<<<<<<< Updated upstream
        for stop_offset, stop in enumerate(self._route_stops):
            node_index = stop_offset + 1  # el índice 0 es el depot
            routing_index = manager.NodeToIndex(node_index)
            time_dimension.CumulVar(routing_index).SetRange(
                max(stop.time_window_start_sec, 0), max(stop.time_window_end_sec, 1)
            )

        pickup_index_by_order = {
            stop.order_id: offset + 1
            for offset, stop in enumerate(self._route_stops)
            if stop.is_pickup
        }
        dropoff_index_by_order = {
            stop.order_id: offset + 1
            for offset, stop in enumerate(self._route_stops)
            if not stop.is_pickup
        }

        solver = routing.solver()
        for order in active_orders:
            pickup_node_index = pickup_index_by_order[order.order_id]
            dropoff_node_index = dropoff_index_by_order[order.order_id]
            pickup_routing_index = manager.NodeToIndex(pickup_node_index)
            dropoff_routing_index = manager.NodeToIndex(dropoff_node_index)

            routing.AddPickupAndDelivery(pickup_routing_index, dropoff_routing_index)
            solver.Add(
                routing.VehicleVar(pickup_routing_index)
                == routing.VehicleVar(dropoff_routing_index)
            )
            solver.Add(
                time_dimension.CumulVar(pickup_routing_index)
                <= time_dimension.CumulVar(dropoff_routing_index)
            )

=======
        base_time_sec = driver_state.current_time_sec
        order_index = {order.order_id: i for i, order in enumerate(active_orders)}
        for order in active_orders:
            i = order_index[order.order_id]
            pickup_idx = manager.NodeToIndex(1 + 2 * i)
            dropoff_idx = manager.NodeToIndex(2 + 2 * i)

            routing.AddPickupAndDelivery(pickup_idx, dropoff_idx)
            routing.solver().Add(
                routing.VehicleVar(pickup_idx) == routing.VehicleVar(dropoff_idx)
            )
            routing.solver().Add(
                time_dimension.CumulVar(pickup_idx) <= time_dimension.CumulVar(dropoff_idx)
            )

            due_sec = max(0, order.due_time_sec - base_time_sec)
            time_dimension.CumulVar(pickup_idx).SetRange(0, due_sec)
            time_dimension.CumulVar(dropoff_idx).SetRange(0, due_sec)

>>>>>>> Stashed changes
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
<<<<<<< Updated upstream
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.FromMilliseconds(150)

        solution = routing.SolveWithParameters(search_parameters)
        if solution is None:
            logger.warning(
                "OR-Tools no encontró solución factible para %d pedidos activos; "
                "se usa orden FIFO como respaldo seguro.",
                len(active_orders),
            )
            return [order.order_id for order in active_orders]

        ordered_order_ids: List[int] = []
        seen_order_ids: set[int] = set()
        index = routing.Start(0)
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            if node_index != 0:
                stop = self._route_stops[node_index - 1]
                if not stop.is_pickup and stop.order_id not in seen_order_ids:
                    ordered_order_ids.append(stop.order_id)
                    seen_order_ids.add(stop.order_id)
            index = solution.Value(routing.NextVar(index))

        for order in active_orders:
            if order.order_id not in seen_order_ids:
                ordered_order_ids.append(order.order_id)

        return ordered_order_ids
=======
        search_parameters.time_limit.FromMilliseconds(300)

        solution = routing.SolveWithParameters(search_parameters)
        if solution is None:
            raise RuntimeError("OR-Tools no encontro una solucion factible para el PDPTW.")

        node_to_order_id = {1 + 2 * i: order.order_id for i, order in enumerate(active_orders)}
        sequence: List[int] = []
        seen: set[int] = set()
        index = routing.Start(0)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            order_id = node_to_order_id.get(node)
            if order_id is not None and order_id not in seen:
                seen.add(order_id)
                sequence.append(order_id)
            index = solution.Value(routing.NextVar(index))

        if len(sequence) != len(active_orders):
            raise RuntimeError("La solucion de OR-Tools no visito todos los pedidos activos.")
        return sequence
>>>>>>> Stashed changes
