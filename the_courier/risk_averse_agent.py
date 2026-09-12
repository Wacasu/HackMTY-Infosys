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
from risk_model import candidate_pickup_origins
from risk_model import point_risk as _shared_point_risk
from risk_model import route_risk_and_time as _shared_route_risk_and_time
from risk_model import weighted_route_risk

logger = logging.getLogger("the_courier.risk_averse_agent")

# Cuántos "minutos equivalentes" de penalización representa un riesgo
# máximo (Re = 1.0) antes de multiplicarse por alpha. Se usa UNA VEZ por
# viaje completo en `evaluate_offer`/`_solve_pdptw` (decisión de aceptar y
# secuenciar pedidos) -- ahí SÍ tiene sentido como cuota fija por viaje,
# independiente de su duración.
#
# Antes en 45.0: con lluvia activa, `point_risk` sube el riesgo AMBIENTAL
# (no solo cerca de zonas inundables -- en TODA la ciudad) a ~0.33 con
# severidad 0.8. A alpha=0.35, eso ya son 0.35*0.33*45 ≈ 5.2 min de
# penalización sobre CUALQUIER viaje, en cualquier parte -- medido en vivo,
# esto bastaba para tumbar la rentabilidad de casi todos los pedidos por
# debajo del umbral y el agente rechazaba el 100% (40/40) en cuanto había
# lluvia, sin aceptar ni uno solo -- lo opuesto a "más cauteloso", más bien
# "deja de operar por completo". El límite duro de seguridad
# (SAFETY_HARD_RISK_LIMIT, en risk_model.py) ya se encarga de rechazar sin
# excepción las rutas genuinamente peligrosas (cerca de una zona
# inundable); este penalty solo debe desalentar marginalmente los tramos
# de riesgo ambiental parejo, no anular el negocio entero.
RISK_PENALTY_MINUTES_AT_MAX_RISK = 20.0

# Cuánto se infla proporcionalmente el tiempo PERCIBIDO de una arista bajo
# riesgo máximo (Re=1.0) y alpha=1.0, usado SOLO por `risk_weighted_path`
# para elegir la ruta física real arista por arista -- deliberadamente
# distinto de RISK_PENALTY_MINUTES_AT_MAX_RISK (que es una cuota fija por
# VIAJE completo, no por arista): sumar una cuota fija a cada arista sesga
# a Dijkstra hacia rutas con menos tramos aunque sean más lentas en total,
# incluso con riesgo uniforme. Ver el comentario en `risk_weighted_path`.
RISK_TIME_INFLATION_AT_MAX_RISK = 4.0

# Rentabilidad mínima aceptable en MXN por minuto ajustado por riesgo.
#
# A propósito IGUAL al umbral de Greedy (`greedy_agent.MIN_ACCEPTABLE_MXN_PER_MINUTE`
# = 3.0), no más bajo. Antes era 2.5: con clima despejado, el término de
# riesgo (alpha * Re_ambiental * RISK_PENALTY_MINUTES_AT_MAX_RISK ≈
# 0.35*0.05*45 ≈ 0.8 min) es casi nulo, así que ese umbral más bajo NO
# reflejaba "tolera más riesgo" -- reflejaba "acepta ofertas más flojas en
# general", sin importar el riesgo. Eso hacía que aceptara más pedidos de
# los que alcanzaba a entregar en el turno (medido en vivo: 8 aceptados,
# solo 3 completados, 5 atorados en cola sin cobrar nada), perdiendo
# contra Greedy por pura sobre-aceptación, no por ser "más seguro". Con el
# mismo umbral base, la única diferencia real entre los dos agentes vuelve
# a ser cómo manejan el riesgo -- que es precisamente lo que este proyecto
# se propone comparar -- no cuál es más permisivo en general.
MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE = 3.0

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
            # PROPORCIONAL al tiempo real del tramo, NO una cuota fija por
            # arista. Sumar una constante fija por arista (como se hacía
            # antes) sesga a Dijkstra hacia rutas con MENOS tramos aunque
            # sean más lentas en total -- incluso con riesgo uniforme (sin
            # ningún evento activo, cada nodo mide exactamente el mismo
            # 0.05 ambiental), porque cada arista adicional paga la misma
            # penalización sin importar cuánto dure. Medido en vivo: eso
            # hacía que Risk-averse manejara rutas reales más lentas que el
            # camino más rápido incluso en clima despejado -- sin ganar
            # ninguna seguridad real a cambio (no hay ninguna zona más
            # riesgosa que evitar si el riesgo es parejo en todos lados) --
            # y perdía rendimiento frente a Greedy solo por eso. Escalado
            # por `travel_time_sec`, un riesgo uniforme infla TODAS las
            # aristas por el mismo factor proporcional (no cambia qué ruta
            # es más rápida), y solo cuando el riesgo varía de verdad entre
            # tramos (p. ej. una zona inundable durante lluvia) el desvío
            # se vuelve realmente más barato que atravesarla.
            return travel_time_sec * (1.0 + alpha * edge_risk * RISK_TIME_INFLATION_AT_MAX_RISK)

        return await self._graph_provider.shortest_path_weighted(
            origin_node, dest_node, edge_weight
        )

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
        #
        # La pierna de recogida, además, se evalúa desde varios orígenes
        # candidatos (posición actual + dropoffs de pedidos ya aceptados) y
        # se queda con el más barato: si este pickup cae cerca de un
        # dropoff que el repartidor ya trae en curso, la decisión lo debe
        # reflejar como "casi gratis llegar" en vez de medirlo siempre
        # desde la posición física de ESTE instante, que puede estar del
        # otro lado de la ciudad mientras termina lo que ya trae.
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
        risk_adjusted_score = offer.base_fare_mxn / max(weighted_cost_minutes, 0.1)

        hard_safety_violation = weighted_risk >= SAFETY_HARD_RISK_LIMIT
        meets_profitability_bar = (
            risk_adjusted_score >= MIN_ACCEPTABLE_RISK_ADJUSTED_MXN_PER_MINUTE
        )

        # Factibilidad dura: si ni siquiera arrancando AHORA MISMO (sin
        # contar ningún otro pedido ya en cola) se llega a tiempo, no hay
        # rentabilidad que lo justifique -- aceptarlo sería prometer una
        # entrega que ya sabe que va a incumplir. Antes no existía este
        # chequeo: se aceptaban ofertas rentables sin importar si la
        # ventana de tiempo ya era matemáticamente imposible, lo que
        # producía pedidos entregados hasta 26 minutos tarde sin que el
        # modelo lo reconociera.
        projected_completion_sec = driver_state.current_time_sec + total_travel_sec
        infeasible = projected_completion_sec > offer.due_time_sec

        accepted = meets_profitability_bar and not hard_safety_violation and not infeasible

        net_profit_estimate = offer.base_fare_mxn - (
            weighted_risk * OPERATIONAL_RISK_COST_MXN_PER_RISK_UNIT
        )

        if infeasible:
            reasoning = (
                f"Rechazado: no llegaría a tiempo -- completaría en el segundo "
                f"{projected_completion_sec:.0f} pero la ventana vence en el "
                f"{offer.due_time_sec}, aun arrancando de inmediato."
            )
        elif hard_safety_violation:
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

        # Secuencial A PROPÓSITO, NO `asyncio.gather` -- esto solía
        # despachar las hasta 42 parejas (3 pedidos activos = 7 nodos) en
        # paralelo, pero cada una es Dijkstra puro-Python (GIL-bound) sobre
        # el grafo real: "paralelizar" con gather no las corre de verdad en
        # paralelo, solo hace que 42 hilos se turnen el mismo núcleo, y el
        # overhead de cambio de contexto entre tantos hilos terminaba
        # constando MÁS que resolverlas una por una (medido en vivo: la
        # versión con gather disparaba seguido el timeout duro de
        # `ROUTE_PLANNING_TIMEOUT_SEC` -- 3+ segundos reales de "freeze"
        # visible en el mapa -- mientras que en secuencia cada pareja tarda
        # ~15-30ms, bien por debajo de ese límite incluso con 42 de ellas).
        # Mismo diagnóstico y mismo arreglo que ya se aplicó al loop de
        # evaluación de ofertas en `server.py::_tick`.
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

            # Ventana [ready_time, due_time] relativa al instante actual del
            # turno, no solo el límite superior: un PDPTW completo respeta
            # ambos extremos. En la práctica todo pedido en `active_orders`
            # ya está "listo" para cuando llega aquí (se aceptó porque su
            # ready_time ya había pasado), así que el límite inferior casi
            # siempre queda en 0 -- se deja explícito de todos modos por
            # completitud del modelo.
            ready_sec = max(0, order.ready_time_sec - base_time_sec)
            due_sec = max(ready_sec, order.due_time_sec - base_time_sec)
            time_dimension.CumulVar(pickup_idx).SetRange(ready_sec, due_sec)
            time_dimension.CumulVar(dropoff_idx).SetRange(ready_sec, due_sec)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        # Búsqueda local guiada además de la heurística de primera solución:
        # con instancias tan chicas (tope de MAX_BATCH_ORDERS_FOR_ROUTE_PLANNING
        # pedidos activos por lote, ver server.py) sobra presupuesto de
        # tiempo para que mejore la solución inicial en vez de quedarse con
        # la primera que encuentra -- rutas mejores, no solo factibles.
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.FromMilliseconds(300)

        solution = routing.SolveWithParameters(search_parameters)
        if solution is None:
            raise RuntimeError("OR-Tools no encontro una solucion factible para el PDPTW.")

        # OJO: se registra el order_id la PRIMERA vez que aparece en la ruta
        # (su nodo de RECOGIDA, que siempre precede a su entrega) -- no la
        # última. El motor de simulación solo consume `sequence[0]` como "el
        # próximo pedido a recoger y entregar antes de replanificar", así
        # que el orden correcto es por recogida, no por entrega: si se
        # armara por entrega, en una ruta que intercala recogidas (p. ej.
        # recoger A, recoger B, entregar A, entregar B) el motor terminaría
        # yendo primero al pedido equivocado.
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
