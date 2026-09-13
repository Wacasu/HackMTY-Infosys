"""
risk_averse_agent.py  (PASO 3)
================================
Agente Inteligente del reto "The Courier": alpha > 0.

Aceptación OBLIGATORIA (igual que Greedy): `evaluate_offer` ya no rechaza
nada, ni por rentabilidad ni por el límite duro de riesgo. Lo que sigue
distinguiendo a este agente es:

  1. `risk_weighted_path`: la ruta FÍSICA que de verdad recorre para cada
     pedido minimiza Te + alpha*Re por arista, no solo Te -- así, para el
     MISMO pedido que Greedy también entrega, este agente tiende a exponer
     al repartidor a menos riesgo real (y por lo tanto gana más Bonus de
     Riesgo, ver risk_model.risk_bonus_points).
  2. `plan_route`/`_solve_pdptw`: ruteo orientado a PUNTOS, no solo a
     factibilidad. Con aceptación obligatoria y sin filtro de admisión, la
     cola de pedidos activos puede crecer más rápido de lo que un solo
     repartidor alcanza a vaciar en lo que queda del turno; en vez de
     exigir que el solver sirva a TODOS (y fallar sin solución si no
     caben), se le permite declinar temporalmente los de menor densidad de
     valor, maximizando los puntos cobrables dentro de la ventana de
     tiempo real que resta -- los que quedan fuera se reconsideran en la
     siguiente replanificación, o terminan en `points_lost_timeout` si el
     turno se acaba antes de que les toque turno.
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
from risk_model import candidate_pickup_origins
from risk_model import point_risk as _shared_point_risk
from risk_model import risk_bonus_points, RISK_BONUS_MAX_POINTS
from risk_model import route_risk_and_time as _shared_route_risk_and_time
from risk_model import weighted_route_risk

logger = logging.getLogger("the_courier.risk_averse_agent")

# Cuánto se infla proporcionalmente el tiempo PERCIBIDO de una arista bajo
# riesgo máximo (Re=1.0) y alpha=1.0, usado por `risk_weighted_path` para
# elegir la ruta física real arista por arista. Proporcional al tiempo real
# del tramo (no una cuota fija) para no sesgar a Dijkstra hacia rutas con
# menos tramos aunque sean más lentas -- ver el comentario en
# `risk_weighted_path` para el detalle completo de por qué.
RISK_TIME_INFLATION_AT_MAX_RISK = 4.0

MIN_TRAVEL_TIME_FLOOR_SEC = 30.0

# Cuántos "minutos equivalentes" de penalización representa un riesgo
# máximo (Re = 1.0) antes de multiplicarse por alpha -- se usa en la
# matriz de costos de `prepare_route_matrix` para que la SECUENCIA elegida
# también evite zonas de riesgo, no solo minimice tiempo.
RISK_PENALTY_MINUTES_AT_MAX_RISK = 20.0

# Escala (segundos de "costo" por punto de valor) que convierte el valor
# de un pedido (`base_points` + el Bonus de Riesgo máximo posible) en la
# penalización que paga el solver de OR-Tools por DECLINARLO temporalmente
# en `_solve_pdptw`. Calibrado para que declinar un pedido de valor típico
# salga más caro que la mayoría de los desvíos entre paradas (cientos a
# ~20 minutos de trayecto real) -- así el solver solo lo declina cuando de
# verdad no cabe en la ventana de tiempo que resta del turno, no por
# conveniencia menor.
POINTS_TO_SECONDS_SCALE = 30.0


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
    Agente Inteligente (alpha > 0): aceptación obligatoria, pero rutea
    Te + alpha*Re tanto en la secuencia (OR-Tools) como en el camino físico
    (Dijkstra ponderado), maximizando puntos dentro del tiempo del turno.
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
    # `risk_model.py`, para que el KPI de riesgo promedio sea una
    # comparación justa entre los dos agentes -- ver ahí el porqué).
    # ------------------------------------------------------------------ #
    def _point_risk(self, lat: float, lon: float, shift_state: ShiftState) -> float:
        return _shared_point_risk(lat, lon, shift_state)

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
        consulta. Precalcular el riesgo de los ~8 mil nodos UNA vez por
        cambio de clima (no por arista) convierte cada evaluación en un
        lookup de diccionario."""
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
        ruta elegida se desvía de esa zona -- y esa menor exposición real
        es lo que se traduce en más Bonus de Riesgo al completar el
        pedido, ya que la aceptación en sí es obligatoria para los dos
        agentes. `city_graph.py` la detecta por duck-typing
        (`getattr(agent, "risk_weighted_path", None)`) para decidir si el
        Greedy (ciego al riesgo) sigue con la ruta más rápida a secas."""
        if self.alpha <= 0.0:
            return await self._graph_provider.shortest_path(origin_node, dest_node)

        node_risk = self._node_risk_map(shift_state)
        alpha = self.alpha

        def edge_weight(u: int, v: int, edge_datas: dict) -> float:
            best = min(
                edge_datas.values(),
                key=lambda data: float(data.get("travel_time_sec", float("inf"))),
            )
            travel_time_sec = float(best.get("travel_time_sec", float("inf")))
            if not math.isfinite(travel_time_sec):
                return float("inf")
            edge_risk = (node_risk.get(u, 0.05) + node_risk.get(v, 0.05)) / 2.0
            # PROPORCIONAL al tiempo real del tramo, no una cuota fija por
            # arista -- ver el razonamiento completo en el historial del
            # módulo: una cuota fija sesga a Dijkstra hacia rutas con menos
            # tramos aunque sean más lentas, incluso con riesgo uniforme.
            return travel_time_sec * (1.0 + alpha * edge_risk * RISK_TIME_INFLATION_AT_MAX_RISK)

        return await self._graph_provider.shortest_path_weighted(
            origin_node, dest_node, edge_weight
        )

    # ------------------------------------------------------------------ #
    # Evaluación de ofertas -- ACEPTACIÓN OBLIGATORIA
    # ------------------------------------------------------------------ #
    async def evaluate_offer(
        self, offer: Offer, driver_state: DriverState, shift_state: ShiftState
    ) -> AgentDecision:
        self._last_shift_state = shift_state

        # Aceptación obligatoria: no hay umbral de rentabilidad ni de
        # riesgo que pueda rechazar esta oferta. Todo lo que sigue
        # calculando (Te, riesgo, score) es informativo -- alimenta el
        # Bonus de Riesgo estimado, el reasoning del feed y el panel de
        # KPIs, nunca una decisión de aceptar/rechazar.
        #
        # Las dos piernas (a recoger, y de recoger a entregar) se evalúan
        # en paralelo -- la diferencia entre ~2x y ~1x el costo de un solo
        # Dijkstra dentro del presupuesto de 200 ms por decisión -- y la
        # pierna de recogida, además, desde varios orígenes candidatos
        # (posición actual + dropoffs de pedidos ya en cola), quedándose
        # con el más barato.
        candidate_origins = candidate_pickup_origins(driver_state, offer)
        pickup_leg_results, (risk_delivery, time_delivery_sec) = await asyncio.gather(
            asyncio.gather(*(
                self._route_risk_and_time(origin, offer.pickup_node, shift_state)
                for origin in candidate_origins
            )),
            self._route_risk_and_time(offer.pickup_node, offer.dropoff_node, shift_state),
        )
        risk_to_pickup, time_to_pickup_sec = min(pickup_leg_results, key=lambda pair: pair[1])

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
        # "Score" informativo (densidad de valor): ya no decide nada, pero
        # sigue siendo útil para el feed de la demo y como referencia de
        # qué tan bueno fue este pedido en particular.
        value_density_score = offer.base_points / max(weighted_cost_minutes, 0.1)
        estimated_bonus = risk_bonus_points(weighted_risk)
        exceeds_safety_threshold = weighted_risk >= SAFETY_HARD_RISK_LIMIT

        reasoning = (
            f"Aceptado (aceptación obligatoria): Te({te_minutes:.1f} min) + "
            f"alpha({self.alpha:.2f}) * Re({weighted_risk:.2f}) = "
            f"{weighted_cost_minutes:.1f} min-equiv; densidad de valor "
            f"{value_density_score:.2f} pts/min-equiv -- bonus de riesgo "
            f"estimado {estimated_bonus:.1f} pts"
            + (" (ruta de alto riesgo)." if exceeds_safety_threshold else ".")
        )

        return AgentDecision(
            order_id=offer.order_id,
            score=value_density_score,
            estimated_travel_time_sec=total_travel_sec,
            estimated_risk=weighted_risk,
            points_bonus_estimate=estimated_bonus,
            reasoning=reasoning,
            exceeds_safety_threshold=exceeds_safety_threshold,
        )

    # ------------------------------------------------------------------ #
    # Ruteo y batching multi-pedido (OR-Tools, PDPTW orientado a puntos)
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

        # Secuencial A PROPÓSITO, NO `asyncio.gather` -- cada pareja es
        # Dijkstra puro-Python (GIL-bound) sobre el grafo real:
        # "paralelizar" con gather no las corre de verdad en paralelo, solo
        # hace que se turnen el mismo núcleo, y el overhead de cambio de
        # contexto entre tantos hilos termina costando MÁS que resolverlas
        # una por una (medido en vivo: con gather, timeouts reales de 3+
        # segundos; en secuencia, ~15-30ms por pareja).
        pairs = [
            (i, j, origin, dest)
            for i, origin in enumerate(nodes)
            for j, dest in enumerate(nodes)
            if i != j
        ]
        for i, j, origin, dest in pairs:
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
        """Orden en que se atienden los pedidos activos, priorizando
        maximizar puntos dentro del tiempo real que queda del turno.

        Con 0 o 1 pedido no hay nada que decidir. Con 2+ resuelve un
        pickup-and-delivery con ventanas de tiempo (PDPTW) orientado a
        puntos sobre la matriz de costo Te + alpha*Re calculada por
        `prepare_route_matrix`, usando Google OR-Tools cuando el problema
        lo amerita (3+ pedidos activos) y un vecino-más-cercano sobre esa
        misma matriz para el caso simple de 2 pedidos o si OR-Tools no
        encuentra ninguna solución. El motor de simulación solo ejecuta el
        primer pedido de la secuencia devuelta y vuelve a llamar a este
        método al completarlo -- una secuencia MÁS CORTA que
        `active_orders` es válida y esperada: los pedidos que no entraron
        se reconsideran en la siguiente replanificación.
        """
        if not active_orders:
            return []
        if len(active_orders) == 1 or not self._route_cost_matrix or not self._route_stops:
            return [order.order_id for order in active_orders]

        if len(active_orders) >= 3:
            try:
                sequence = self._solve_pdptw(driver_state, active_orders)
                if sequence:
                    return sequence
                # Secuencia VACÍA: el solver declinó absolutamente todo, lo
                # que pasa de forma sistemática en el último tramo del
                # turno (ya no queda ventana en la que quepa nada). Pero
                # devolver [] aquí deja al repartidor parado en la calle
                # con la bolsa llena el resto del turno, sin cobrar nada --
                # medido en vivo: se congelaba en 4 entregas con 8 pedidos
                # en cola mientras Greedy seguía repartiendo. Entregar
                # tarde sigue pagando los puntos base (y el KPI "Tarde" lo
                # reporta con honestidad), así que siempre es mejor que
                # quedarse quieto: se cae al vecino más cercano, que nunca
                # devuelve vacío.
                logger.info(
                    "El ruteo orientado a puntos declino todos los pedidos "
                    "(sin ventana viable en lo que resta del turno); se sigue "
                    "repartiendo por vecino mas cercano para no dejar al "
                    "repartidor detenido."
                )
            except Exception as exc:
                # Camino esperado y manejado (no un bug): en casos límite
                # OR-Tools puede no encontrar ninguna solución ni siquiera
                # declinando pedidos. Se registra en INFO sin traceback --
                # un ERROR con stack trace aquí se ve como una caída real
                # en la consola del servidor durante una demo, cuando en
                # realidad el respaldo de abajo ya lo resuelve sin
                # interrumpir la sesión.
                logger.info(
                    "OR-Tools no encontro solucion para el PDPTW orientado a "
                    "puntos (%s); usando vecino mas cercano sobre la matriz "
                    "de costo ponderada por riesgo como respaldo.",
                    exc,
                )

        return self._nearest_neighbor_sequence(active_orders)

    def _nearest_neighbor_sequence(self, active_orders: List[Offer]) -> List[int]:
        """Recorre, de forma golosa, el pedido cuyo *pickup* es más barato
        (Te + alpha*Re) desde la posición actual, encadenando desde el
        *dropoff* del pedido recién elegido. Usa el riesgo real de la ruta
        para decidir cuál pedido conviene atender primero."""
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

        # Ventana de tiempo REAL que queda del turno -- ya no un tope
        # genérico de 24h. Con aceptación obligatoria y sin filtro de
        # admisión, el ruteo orientado a puntos necesita saber exactamente
        # cuánto tiempo real le queda para decidir qué de verdad alcanza a
        # cumplir dentro del turno, no de forma abstracta.
        shift_state = self._last_shift_state
        remaining_shift_sec = 24 * 3600
        if shift_state is not None:
            remaining_shift_sec = max(
                0, shift_state.shift_duration_sec - driver_state.current_time_sec
            )

        # Horizonte del modelo. Nunca puede ser 0 ni menor que la ventana
        # más lejana que se le vaya a pedir a una parada: un horizonte más
        # corto que un `SetRange` deja el dominio de esa variable vacío y
        # OR-Tools trata eso como error del modelo, no como "sin solución".
        horizon_sec = max(int(remaining_shift_sec), 600)

        routing.AddDimension(
            transit_callback_index,
            int(RISK_PENALTY_MINUTES_AT_MAX_RISK * 60.0 * 4),  # holgura (slack)
            horizon_sec,
            True,  # el acumulado empieza en 0 en la posición actual
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")

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

            # Ventana de tiempo, siempre saneada a un rango válido dentro
            # del horizonte: un `SetRange` con mínimo > máximo (pedido ya
            # vencido, o que abre después de que se acaba el turno) vacía
            # el dominio de la variable y OR-Tools aborta el proceso a
            # nivel C++ en vez de devolver "sin solución". Un pedido cuya
            # ventana ya no cabe simplemente se queda sin holgura y el
            # solver lo declina vía la disyunción de abajo, que es el
            # comportamiento correcto y seguro.
            ready_sec = min(max(0, order.ready_time_sec - base_time_sec), horizon_sec)
            due_sec = max(ready_sec, order.due_time_sec - base_time_sec)
            window_end_sec = min(due_sec, horizon_sec)
            window_end_sec = max(window_end_sec, ready_sec)
            time_dimension.CumulVar(pickup_idx).SetRange(ready_sec, window_end_sec)
            time_dimension.CumulVar(dropoff_idx).SetRange(ready_sec, window_end_sec)

            # Ruteo orientado a puntos: en vez de exigir que el par
            # pickup+dropoff se visite SIEMPRE (lo que antes hacía que
            # OR-Tools fallara sin solución si no cabían todos en la
            # ventana), se le permite al solver "declinarlo" a cambio de
            # pagar una penalización igual al valor en puntos que costaría
            # dejarlo fuera. Así prioriza la mayor densidad de valor dentro
            # del tiempo real que queda, en vez de tronar sin solución --
            # los pedidos declinados se reconsideran en la siguiente
            # replanificación, o terminan en `points_lost_timeout` si el
            # turno se acaba antes de que les toque turno.
            order_value_points = order.base_points + RISK_BONUS_MAX_POINTS
            drop_penalty = int(order_value_points * POINTS_TO_SECONDS_SCALE)
            # El tercer argumento (max_cardinality=2) NO es opcional aquí:
            # por defecto es 1, que significa "visita como mucho UNO de
            # estos dos nodos" -- justo lo contrario de lo que se necesita.
            # Combinado con el AddPickupAndDelivery de arriba (que obliga a
            # visitar los dos), deja el modelo en contradicción directa, y
            # OR-Tools no lanza una excepción de Python ante eso: aborta el
            # proceso entero a nivel C++, tumbando el worker de uvicorn sin
            # dejar traceback (medido: el turno se congelaba y el servidor
            # dejaba de responder incluso /health). Con 2 la disyunción
            # dice lo correcto: "sirve AMBOS nodos, o ninguno y paga la
            # penalización".
            routing.AddDisjunction([pickup_idx, dropoff_idx], drop_penalty, 2)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        # Búsqueda local guiada además de la heurística de primera
        # solución: con instancias tan chicas (tope de
        # MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING pedidos activos por lote, ver
        # server.py) sobra presupuesto de tiempo para que mejore la
        # solución inicial en vez de quedarse con la primera que
        # encuentra -- rutas mejores, no solo factibles.
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.FromMilliseconds(300)

        solution = routing.SolveWithParameters(search_parameters)
        if solution is None:
            raise RuntimeError(
                "OR-Tools no encontro ninguna solucion (ni siquiera declinando pedidos)."
            )

        # OJO: se registra el order_id la PRIMERA vez que aparece en la
        # ruta (su nodo de RECOGIDA, que siempre precede a su entrega) --
        # no la última. El motor de simulación solo consume `sequence[0]`
        # como "el próximo pedido a recoger y entregar antes de
        # replanificar", así que el orden correcto es por recogida, no por
        # entrega.
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

        # A diferencia de antes, una secuencia MÁS CORTA que `active_orders`
        # ya no es un error -- es la señal de que el ruteo orientado a
        # puntos decidió, a propósito, declinar algunos pedidos por ahora
        # (el `AddDisjunction` de arriba) porque no caben con buena
        # densidad de valor en el tiempo que resta. Quedan en la cola para
        # la siguiente replanificación.
        return sequence
