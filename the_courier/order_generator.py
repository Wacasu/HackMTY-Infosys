"""
order_generator.py  (PASO 1)
=============================
Generador de pedidos determinista para el simulador "The Courier".

Produce un dataset sintético con la misma estructura que los benchmarks
clásicos de Solomon para VRPTW (ID, coordenadas, ventana de tiempo
[ready_time, due_date], tiempo de servicio y demanda), pero georreferenciado
dentro del área real de Monterrey y, obligatoriamente, proyectado sobre el
componente fuertemente conexo del grafo vial de OSMnx antes de exponerse a
los agentes.

Determinismo (regla obligatoria)
---------------------------------
Toda la generación depende exclusivamente de `random_seed`. Dos instancias
de `OrderGenerator` creadas con el mismo `random_seed` y el mismo
`CityGraphProvider` producen exactamente la misma secuencia de pedidos, con
los mismos nodos de pickup/dropoff, tarifas y ventanas de tiempo. Esto es lo
que permite que el Agente Base y el Agente Inteligente compitan sobre
EXACTAMENTE el mismo turno no visto.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Dict, List

from agent_interface import Offer
from city_graph import CityGraphProvider

logger = logging.getLogger("the_courier.order_generator")

# Parámetros tarifarios base (moneda: MXN), calibrados a un rango realista
# para repartos de última milla en Monterrey.
BASE_PICKUP_FEE_MXN = 12.0
FARE_PER_KM_MXN = 6.5
MIN_SERVICE_TIME_SEC = 90
MAX_SERVICE_TIME_SEC = 240
MIN_TIME_WINDOW_SLACK_SEC = 600
MAX_TIME_WINDOW_SLACK_SEC = 2400

EARTH_RADIUS_KM = 6371.0
<<<<<<< Updated upstream
CENTRO_MONTERREY_BOUNDS = (25.650, 25.695, -100.345, -100.285)
=======
# Centro de Monterrey (Macroplaza y alrededores, ~4.5 km de medio-lado desde
# el depósito) en vez de la zona metropolitana completa: acota las
# pruebas/demos a una zona chica y siempre dentro del grafo vial que carga
# `city_graph.py` (radio `CENTRO_MONTERREY_RADIUS_M` = 6 km, dejando ~1.5 km
# de margen para que ningún pedido caiga cerca del borde del grafo). Las
# coordenadas se proyectan despues a calles reales.
CENTRO_MONTERREY_BOUNDS = (25.6309, 25.7119, -100.3542, -100.2642)
>>>>>>> Stashed changes


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en línea recta entre dos coordenadas. Se usa ÚNICAMENTE
    como referencia informativa para mostrar en el panel del simulador;
    jamás se usa para decidir rutas ni tiempos de viaje, que siempre se
    obtienen del grafo vial real vía `CityGraphProvider`."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class OrderGenerator:
    """
    Genera y sirve pedidos deterministas, ya proyectados a nodos válidos del
    grafo vial de Monterrey.

    Uso:
        provider = await get_city_graph_provider()
        generator = await OrderGenerator.create(
            random_seed=42, graph_provider=provider, num_orders=60
        )
        pending = generator.get_pending_orders(current_time_sec=900)
    """

    def __init__(self, random_seed: int, shift_duration_sec: int) -> None:
        self.random_seed = random_seed
        self.shift_duration_sec = shift_duration_sec
        self._orders: List[Offer] = []
        self._orders_by_id: Dict[int, Offer] = {}
        self._taken_order_ids: set[int] = set()

    @classmethod
    async def create(
        cls,
        random_seed: int,
        graph_provider: CityGraphProvider,
        num_orders: int = 60,
        shift_duration_sec: int = 6 * 3600,
    ) -> "OrderGenerator":
        """Fábrica asíncrona: genera coordenadas deterministas y las
        proyecta sobre el grafo real (I/O-bound vía OSMnx) antes de que el
        generador quede listo para usarse."""
        instance = cls(random_seed=random_seed,
                       shift_duration_sec=shift_duration_sec)
        await instance._generate_and_project(graph_provider, num_orders)
        return instance

    async def _generate_and_project(
        self, graph_provider: CityGraphProvider, num_orders: int
    ) -> None:
        await graph_provider.ensure_loaded()
        rng = random.Random(self.random_seed)

        raw_records = self._generate_solomon_style_records(rng, num_orders)

        for record in raw_records:
            pickup_node = await graph_provider.nearest_node(
                record["pickup_lat"], record["pickup_lon"]
            )
            dropoff_node = await graph_provider.nearest_node(
                record["dropoff_lat"], record["dropoff_lon"]
            )

            # Si el sorteo aleatorio coloca pickup y dropoff en el mismo
            # nodo (áreas urbanas densas), forzamos una separación mínima
            # re-muestreando el dropoff para que el pedido tenga sentido.
            attempts = 0
            while dropoff_node == pickup_node and attempts < 5:
<<<<<<< Updated upstream
                record["dropoff_lat"], record["dropoff_lon"] = self._random_point_in_center(
=======
                record["dropoff_lat"], record["dropoff_lon"] = self._random_point_in_centro(
>>>>>>> Stashed changes
                    rng)
                dropoff_node = await graph_provider.nearest_node(
                    record["dropoff_lat"], record["dropoff_lon"]
                )
                attempts += 1

            straight_line_km = _haversine_km(
                record["pickup_lat"],
                record["pickup_lon"],
                record["dropoff_lat"],
                record["dropoff_lon"],
            )
            base_fare = round(
                BASE_PICKUP_FEE_MXN + FARE_PER_KM_MXN *
                max(straight_line_km, 0.3), 2
            )

            offer = Offer(
                order_id=record["order_id"],
                pickup_node=pickup_node,
                dropoff_node=dropoff_node,
                pickup_lat=record["pickup_lat"],
                pickup_lon=record["pickup_lon"],
                dropoff_lat=record["dropoff_lat"],
                dropoff_lon=record["dropoff_lon"],
                ready_time_sec=record["ready_time_sec"],
                due_time_sec=record["due_time_sec"],
                service_time_sec=record["service_time_sec"],
                base_fare_mxn=base_fare,
                straight_line_distance_km=round(straight_line_km, 3),
            )
            self._orders.append(offer)
            self._orders_by_id[offer.order_id] = offer

        self._orders.sort(key=lambda o: o.ready_time_sec)
        logger.info(
            "OrderGenerator(seed=%d) listo con %d pedidos proyectados a la malla vial real.",
            self.random_seed,
            len(self._orders),
        )

    @staticmethod
<<<<<<< Updated upstream
    def _random_point_in_center(rng: random.Random) -> tuple[float, float]:
=======
    def _random_point_in_centro(rng: random.Random) -> tuple[float, float]:
>>>>>>> Stashed changes
        min_lat, max_lat, min_lon, max_lon = CENTRO_MONTERREY_BOUNDS
        lat = rng.uniform(min_lat, max_lat)
        lon = rng.uniform(min_lon, max_lon)
        return lat, lon

    def _generate_solomon_style_records(
        self, rng: random.Random, num_orders: int
    ) -> List[dict]:
        """Genera registros con la misma semántica que un archivo Solomon
        VRPTW (customer_id, x, y, ready_time, due_date, service_time), pero
        con x/y sustituidos por coordenadas geográficas reales dentro del
        bounding box de Monterrey."""
        records: List[dict] = []
        for order_id in range(1, num_orders + 1):
<<<<<<< Updated upstream
            pickup_lat, pickup_lon = self._random_point_in_center(rng)
            dropoff_lat, dropoff_lon = self._random_point_in_center(rng)
=======
            pickup_lat, pickup_lon = self._random_point_in_centro(rng)
            dropoff_lat, dropoff_lon = self._random_point_in_centro(rng)
>>>>>>> Stashed changes

            ready_time_sec = rng.randint(
                0, max(self.shift_duration_sec - 1800, 0))
            window_slack = rng.randint(
                MIN_TIME_WINDOW_SLACK_SEC, MAX_TIME_WINDOW_SLACK_SEC
            )
            due_time_sec = min(
                ready_time_sec + window_slack, self.shift_duration_sec
            )
            service_time_sec = rng.randint(
                MIN_SERVICE_TIME_SEC, MAX_SERVICE_TIME_SEC)

            records.append(
                {
                    "order_id": order_id,
                    "pickup_lat": pickup_lat,
                    "pickup_lon": pickup_lon,
                    "dropoff_lat": dropoff_lat,
                    "dropoff_lon": dropoff_lon,
                    "ready_time_sec": ready_time_sec,
                    "due_time_sec": due_time_sec,
                    "service_time_sec": service_time_sec,
                }
            )
        return records

    def get_pending_orders(self, current_time_sec: int) -> List[dict]:
        """Pedidos cuya ventana de tiempo [ready_time, due_time] contiene a
        `current_time_sec` y que aún no han sido tomados por el repartidor.
        Devuelve dicts (serializables directamente a JSON), tal como exige
        la interfaz pública del generador."""
        pending = [
            order
            for order in self._orders
            if order.order_id not in self._taken_order_ids
            and order.ready_time_sec <= current_time_sec <= order.due_time_sec
        ]
        return [order.model_dump() for order in pending]

    def mark_order_taken(self, order_id: int) -> None:
        """El motor de simulación llama a este método cuando un agente
        acepta un pedido, para que deje de ofrecerse (a ese mismo motor)."""
        self._taken_order_ids.add(order_id)

    def get_offer(self, order_id: int) -> Offer:
        """Recupera el objeto `Offer` tipado correspondiente a un pedido,
        para uso interno de los agentes y del motor de simulación."""
        return self._orders_by_id[order_id]

    def list_orders(self) -> List[dict]:
        """Todos los pedidos del turno, serializables para el mapa del panel."""
        return [order.model_dump() for order in self._orders]

    @property
    def total_orders(self) -> int:
        return len(self._orders)
