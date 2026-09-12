"""
city_graph.py
=============
Carga y gestiona el grafo vial real de Monterrey (OSMnx) como un recurso
compartido y 100% asíncrono para todo el simulador "The Courier".

Reglas de arquitectura aplicadas
---------------------------------
1. OSMnx y NetworkX son librerías síncronas y CPU/IO-bound (descarga de
   datos de OpenStreetMap, cómputo de componentes fuertemente conexos,
   Dijkstra). Para no violar la regla de "cero I/O síncrono en el event
   loop", cada llamada a estas librerías se despacha a un hilo worker con
   `asyncio.to_thread`. Ninguna corrutina de FastAPI ni de los agentes
   bloquea jamás esperando a OSMnx directamente.
2. El grafo se reduce, de forma obligatoria, al componente fuertemente
   conexo más grande antes de exponerse. Ningún nodo fuera de ese
   componente es alcanzable desde `nearest_node`, `travel_time_sec` ni
   `shortest_path`.
3. El grafo se carga una única vez por proceso (singleton perezoso,
   protegido con `asyncio.Lock`) y se comparte entre el generador de
   pedidos y ambos agentes, evitando descargas duplicadas y garantizando
   que Greedy y Risk-Averse razonen sobre exactamente la misma malla vial.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import networkx as nx
import osmnx as ox

logger = logging.getLogger("the_courier.city_graph")

MONTERREY_PLACE_NAME = "Monterrey, Nuevo León, México"
MONTERREY_NETWORK_TYPE = "drive"

# Velocidad de respaldo (km/h) para aristas sin atributo `speed_kph` estimado
# por OSMnx a partir de las etiquetas `maxspeed` de OpenStreetMap.
DEFAULT_FALLBACK_SPEED_KPH = 35.0

EARTH_RADIUS_KM = 6371.0

# Zonas de Monterrey propensas a inundación (las mismas que usa el agente
# inteligente). Se usan para penalizar aristas del grafo que pasan cerca.
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


def _node_flood_risk(lat: float, lon: float) -> float:
    """Riesgo base [0, 1] de un nodo por su proximidad a zonas de inundación."""
    max_risk = 0.0
    for _name, z_lat, z_lon, radius_km, base_sev in FLOOD_PRONE_ZONES:
        dist = _haversine_km(lat, lon, z_lat, z_lon)
        if dist <= radius_km:
            proximity = 1.0 - (dist / radius_km)
            max_risk = max(max_risk, proximity * base_sev)
    return max_risk


@dataclass(frozen=True)
class GraphBounds:
    """Límites geográficos (bounding box) del grafo cargado. Se usan para
    generar coordenadas de pedidos que caigan dentro del área vial válida
    antes de proyectarlas a su nodo real más cercano."""

    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float


class CityGraphProvider:
    """
    Proveedor asíncrono y singleton por proceso del grafo vial de Monterrey,
    reducido a su componente fuertemente conexo más grande.
    """

    def __init__(self) -> None:
        self._graph: Optional[nx.MultiDiGraph] = None
        self._bounds: Optional[GraphBounds] = None
        self._load_lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        """Carga el grafo si aún no está en memoria. Es seguro invocarlo
        concurrentemente (p. ej. desde los dos motores de simulación
        arrancando en paralelo): solo la primera corrutina dispara la carga
        real gracias al lock interno; el resto espera y reutiliza el mismo
        grafo ya construido."""
        if self._graph is not None:
            return
        async with self._load_lock:
            if self._graph is not None:
                return
            logger.info("Cargando grafo vial de Monterrey desde OSMnx...")
            self._graph = await asyncio.to_thread(self._build_graph_sync)
            self._bounds = await asyncio.to_thread(
                self._compute_bounds_sync, self._graph
            )
            logger.info(
                "Grafo cargado: %d nodos, %d aristas (componente fuertemente "
                "conexo más grande).",
                self._graph.number_of_nodes(),
                self._graph.number_of_edges(),
            )

    @staticmethod
    def _build_graph_sync(place: str = MONTERREY_PLACE_NAME) -> nx.MultiDiGraph:
        """Descarga el grafo vial de Monterrey y lo reduce al componente
        fuertemente conexo más grande. Función síncrona por diseño: solo
        debe ejecutarse dentro de `asyncio.to_thread`."""
        raw_graph = ox.graph_from_place(
            place, network_type=MONTERREY_NETWORK_TYPE)
        raw_graph = ox.add_edge_speeds(
            raw_graph, fallback=DEFAULT_FALLBACK_SPEED_KPH)
        raw_graph = ox.add_edge_travel_times(raw_graph)

        largest_scc_nodes = max(
            nx.strongly_connected_components(raw_graph), key=len)
        connected_graph = raw_graph.subgraph(largest_scc_nodes).copy()

        for _, _, data in connected_graph.edges(data=True):
            if "travel_time" in data:
                data["travel_time_sec"] = float(data["travel_time"])
            else:
                length_m = float(data.get("length", 1.0))
                speed_kph = float(
                    data.get("speed_kph", DEFAULT_FALLBACK_SPEED_KPH))
                data["travel_time_sec"] = length_m / \
                    (speed_kph * 1000.0 / 3600.0)

        return connected_graph

    @staticmethod
    def _compute_bounds_sync(graph: nx.MultiDiGraph) -> GraphBounds:
        lats = [data["y"] for _, data in graph.nodes(data=True)]
        lons = [data["x"] for _, data in graph.nodes(data=True)]
        return GraphBounds(
            min_lat=min(lats), max_lat=max(lats), min_lon=min(lons), max_lon=max(lons)
        )

    @property
    def is_loaded(self) -> bool:
        return self._graph is not None

    @property
    def graph(self) -> nx.MultiDiGraph:
        if self._graph is None:
            raise RuntimeError(
                "El grafo aún no ha sido cargado. Llama a `await ensure_loaded()` primero."
            )
        return self._graph

    @property
    def bounds(self) -> GraphBounds:
        if self._bounds is None:
            raise RuntimeError(
                "Los límites del grafo aún no están disponibles. Llama a "
                "`await ensure_loaded()` primero."
            )
        return self._bounds

    def node_coordinates(self, node_id: int) -> Tuple[float, float]:
        """Devuelve (lat, lon) de un nodo ya validado del componente conexo."""
        node_data = self.graph.nodes[node_id]
        return float(node_data["y"]), float(node_data["x"])

    async def nearest_node(self, lat: float, lon: float) -> int:
        """Mapea una coordenada (lat, lon) al nodo válido más cercano dentro
        del componente fuertemente conexo, usando `osmnx.distance.nearest_nodes`
        sobre la proyección real del grafo. Prohibido usar distancia
        euclidiana plana para este mapeo."""
        await self.ensure_loaded()
        return await asyncio.to_thread(ox.distance.nearest_nodes, self._graph, lon, lat)

    async def travel_time_sec(self, origin_node: int, dest_node: int) -> float:
        """Tiempo de viaje mínimo (segundos) entre dos nodos del grafo real,
        vía Dijkstra ponderado por `travel_time_sec`."""
        await self.ensure_loaded()
        return await asyncio.to_thread(
            self._shortest_travel_time_sync, origin_node, dest_node
        )

    def _shortest_travel_time_sync(self, origin_node: int, dest_node: int) -> float:
        if origin_node == dest_node:
            return 0.0
        try:
            return nx.shortest_path_length(
                self._graph, origin_node, dest_node, weight="travel_time_sec"
            )
        except nx.NetworkXNoPath:
            return float("inf")

    async def shortest_path(self, origin_node: int, dest_node: int) -> Tuple[int, ...]:
        """Secuencia ordenada de nodos de la ruta más rápida entre dos nodos."""
        await self.ensure_loaded()
        return await asyncio.to_thread(self._shortest_path_sync, origin_node, dest_node)

    async def route_coordinates(self, path: Tuple[int, ...]) -> Tuple[Tuple[float, float], ...]:
        """Devuelve coordenadas (lat, lon) siguiendo la geometria de cada arista OSM."""
        await self.ensure_loaded()
        return await asyncio.to_thread(self._route_coordinates_sync, path)

    def _shortest_path_sync(self, origin_node: int, dest_node: int) -> Tuple[int, ...]:
        if origin_node == dest_node:
            return (origin_node,)
        path = nx.shortest_path(self._graph, origin_node,
                                dest_node, weight="travel_time_sec")
        return tuple(path)

    def _route_coordinates_sync(
        self, path: Tuple[int, ...]
    ) -> Tuple[Tuple[float, float], ...]:
        if not path:
            return ()
        coordinates: list[Tuple[float, float]] = []
        first = self._graph.nodes[path[0]]
        coordinates.append((float(first["y"]), float(first["x"])))
        for origin, destination in zip(path, path[1:]):
            edge_options = self._graph.get_edge_data(origin, destination) or {}
            edge_data = min(
                edge_options.values(),
                key=lambda data: float(
                    data.get("travel_time_sec", float("inf"))),
            )
            geometry = edge_data.get("geometry")
            if geometry is not None:
                edge_coordinates = [(float(lat), float(lon))
                                    for lon, lat in geometry.coords]
                if edge_coordinates and edge_coordinates[0] == coordinates[-1]:
                    edge_coordinates = edge_coordinates[1:]
                coordinates.extend(edge_coordinates)
            else:
                node = self._graph.nodes[destination]
                coordinates.append((float(node["y"]), float(node["x"])))
        return tuple(coordinates)

    async def travel_time_matrix(
        self, nodes: Tuple[int, ...]
    ) -> Tuple[Tuple[float, ...], ...]:
        """Matriz cuadrada de tiempos de viaje (segundos) entre cada par de
        `nodes`, calculada sobre el grafo real. Se usa como matriz de costos
        para el ruteo con OR-Tools (VRPTW) del agente adverso al riesgo."""
        await self.ensure_loaded()
        return await asyncio.to_thread(self._travel_time_matrix_sync, nodes)

    def _travel_time_matrix_sync(
        self, nodes: Tuple[int, ...]
    ) -> Tuple[Tuple[float, ...], ...]:
        matrix: list[list[float]] = []
        for origin in nodes:
            row: list[float] = []
            for dest in nodes:
                row.append(self._shortest_travel_time_sync(origin, dest))
            matrix.append(row)
        return tuple(tuple(row) for row in matrix)

    async def risk_weighted_path(
        self, origin_node: int, dest_node: int, risk_penalty_factor: float
    ) -> Tuple[int, ...]:
        """Ruta que minimiza Te + risk_penalty_factor * riesgo de inundación
        por arista. Con risk_penalty_factor=0 equivale a la ruta más rápida;
        con valores mayores desvía el camino lejos de zonas de inundación."""
        await self.ensure_loaded()
        return await asyncio.to_thread(
            self._risk_weighted_path_sync, origin_node, dest_node, risk_penalty_factor
        )

    def _risk_weighted_path_sync(
        self, origin_node: int, dest_node: int, risk_penalty_factor: float
    ) -> Tuple[int, ...]:
        if origin_node == dest_node:
            return (origin_node,)

        def edge_weight(u: int, v: int, edge_data: dict) -> float:
            travel_sec = float(edge_data.get("travel_time_sec", 1.0))
            u_lat = self._graph.nodes[u]["y"]
            u_lon = self._graph.nodes[u]["x"]
            v_lat = self._graph.nodes[v]["y"]
            v_lon = self._graph.nodes[v]["x"]
            edge_risk = max(_node_flood_risk(u_lat, u_lon), _node_flood_risk(v_lat, v_lon))
            return travel_sec * (1.0 + risk_penalty_factor * edge_risk)

        try:
            path = nx.shortest_path(
                self._graph, origin_node, dest_node, weight=edge_weight
            )
            return tuple(path)
        except nx.NetworkXNoPath:
            return self._shortest_path_sync(origin_node, dest_node)

    async def risk_weighted_travel_time_sec(
        self, origin_node: int, dest_node: int, risk_penalty_factor: float
    ) -> float:
        """Tiempo de viaje real (segundos) de la ruta ponderada por riesgo.
        El tiempo sigue siendo el real de Dijkstra; solo el camino cambia."""
        path = await self.risk_weighted_path(origin_node, dest_node, risk_penalty_factor)
        if len(path) <= 1:
            return 0.0
        total = 0.0
        for u, v in zip(path, path[1:]):
            edge_options = self._graph.get_edge_data(u, v) or {}
            edge_data = min(
                edge_options.values(),
                key=lambda d: float(d.get("travel_time_sec", float("inf"))),
            )
            total += float(edge_data.get("travel_time_sec", 0.0))
        return total


_provider_singleton: Optional[CityGraphProvider] = None
_singleton_lock = asyncio.Lock()


def peek_city_graph_provider() -> Optional[CityGraphProvider]:
    """Devuelve el singleton si ya existe, sin disparar la carga del grafo."""
    return _provider_singleton


async def get_city_graph_provider() -> CityGraphProvider:
    """Punto de acceso único al grafo vial compartido de Monterrey. Devuelve
    siempre la misma instancia (ya cargada) dentro de un mismo proceso."""
    global _provider_singleton
    async with _singleton_lock:
        if _provider_singleton is None:
            _provider_singleton = CityGraphProvider()
        await _provider_singleton.ensure_loaded()
        return _provider_singleton
