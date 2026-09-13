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
from city_graph import (
    CENTRO_MONTERREY_LATITUDE,
    CENTRO_MONTERREY_LONGITUDE,
    CENTRO_MONTERREY_RADIUS_M,
    CityGraphProvider,
)

logger = logging.getLogger("the_courier.order_generator")

# Parámetros del esquema de puntos (antes tarifario en MXN) -- mismos
# números, reinterpretados: ya no representan un pago monetario, sino los
# Puntos Base que otorga completar el pedido, calibrados sobre el mismo
# rango realista de reparto de última milla en Monterrey.
BASE_POINTS = 12.0
POINTS_PER_KM = 6.5
MIN_SERVICE_TIME_SEC = 90
MAX_SERVICE_TIME_SEC = 240

# Antes en 600-2400 (10-40 min). Un viaje típico en el grafo de 6km del
# centro (acercarse al pickup + entregar) mide realistamente entre ~12 y
# ~25 minutos reales -- medido en las decisiones registradas en pruebas
# reales. Con un slack mínimo de solo 10 minutos, una buena parte de los
# pedidos generados quedaban con una ventana de tiempo MATEMÁTICAMENTE
# IMPOSIBLE de cumplir desde el instante en que aparecían (antes de
# agregar el chequeo duro de factibilidad, esto se disimulaba entregando
# tarde en silencio; con el chequeo, el agente los rechaza correctamente
# -- pero eso significa que gran parte del turno no había NINGÚN pedido
# aceptable, y el repartidor se quedaba parado). Subir el slack mínimo a
# 20 min le da a la mayoría de los pedidos una ventana real de ser
# cumplidos incluso con algo de cola por delante, sin dejar de tener
# variedad (algunos pedidos siguen siendo más urgentes que otros).
MIN_TIME_WINDOW_SLACK_SEC = 1200
MAX_TIME_WINDOW_SLACK_SEC = 3000

EARTH_RADIUS_KM = 6371.0

# Centro de Monterrey (Macroplaza y alrededores) para el generador de
# pickups/dropoffs ALEATORIOS -- el comportamiento ACTIVO por defecto (ver
# `FIXED_PICKUPS_ENABLED` más abajo). ~1.5 km de margen respecto al radio
# real del grafo (`CENTRO_MONTERREY_RADIUS_M` = 6 km en city_graph.py)
# para que ningún punto caiga cerca del borde. Las coordenadas se
# proyectan después a calles reales.
CENTRO_MONTERREY_BOUNDS = (25.6309, 25.7119, -100.3542, -100.2642)

# --------------------------------------------------------------------- #
# Pickup fijo en CEDIS reales -- PREPARADO PERO INACTIVO.
# --------------------------------------------------------------------- #
# `FIXED_PICKUP_LOCATIONS` son 9 CEDIS/parques logísticos reales de la
# Zona Metropolitana de Monterrey (Apodaca, Escobedo, Santa Catarina,
# Ciénega de Flores / Salinas Victoria), geocodificados contra
# OpenStreetMap/Nominatim -- no inventados. La idea: cada pedido recoge en
# uno de estos puntos (elegido determinísticamente por `random_seed`) en
# vez de un punto aleatorio cualquiera, con el dropoff aleatorio pero
# relativo a CADA pickup (`_random_point_near` + `_safe_dropoff_radius_km`,
# no una caja fija compartida).
#
# Todos estos CEDIS quedan a 11-34km del centro -- fuera del radio de 6km
# que carga `city_graph.py` hoy (ver `ZMM_PLACE_NAMES` ahí). Activarlos
# con el grafo chico haría que `nearest_node` los colapsara TODOS al borde
# del círculo, silenciosamente -- exactamente el problema de proyección
# que se buscaba evitar. Por eso este flag empieza en `False`: actívalo
# junto con la cobertura de ZMM completa en `city_graph.py` (ver el
# comentario ahí sobre el bloqueo de red pendiente), no antes.
FIXED_PICKUPS_ENABLED = False

FIXED_PICKUP_LOCATIONS: tuple[dict, ...] = (
    {"name": "Interpuerto Monterrey (Salinas Victoria)", "lat": 25.928985, "lon": -100.286737},
    {"name": "Parque Industrial STIVA Aeropuerto (Apodaca)", "lat": 25.779898, "lon": -100.146156},
    {"name": "FINSA Apodaca", "lat": 25.774069, "lon": -100.170289},
    {"name": "Parque Industrial y de Negocios Nexxus XXI (Escobedo)", "lat": 25.771138, "lon": -100.307076},
    {"name": "FINSA II (Santa Catarina)", "lat": 25.717654, "lon": -100.514414},
    {"name": "VYNMSA Santa Catarina Industrial Park", "lat": 25.725834, "lon": -100.518128},
    {"name": "Parque Industrial Kalos (Santa Catarina)", "lat": 25.689297, "lon": -100.483162},
    {"name": "Parque Industrial Las Palmas (Santa Catarina)", "lat": 25.698913, "lon": -100.469809},
    {"name": "Ciénega de Flores (corredor industrial)", "lat": 25.953326, "lon": -100.187424},
)

# Qué tan lejos del CEDIS de origen puede caer un dropoff, en km -- da
# variedad de "última milla" real sin necesitar que el grafo cubra viajes
# absurdamente largos de una punta a otra de la ZMM.
DROPOFF_RADIUS_KM = 6.0

# Margen de seguridad (km) para nunca generar un dropoff más allá del
# radio real del grafo cargado (`CENTRO_MONTERREY_RADIUS_M` en
# city_graph.py) -- sin esto, un CEDIS ya cerca del borde del grafo
# (p. ej. Ciénega de Flores) podría generar dropoffs FUERA del área
# cargada, que `nearest_node` colapsaría silenciosamente al nodo más
# cercano disponible (el mismo problema de proyección que motivó fijar
# estos pickups con coordenadas reales en primer lugar).
GRAPH_RADIUS_SAFETY_MARGIN_KM = 1.5


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
            pickup_lat, pickup_lon = graph_provider.node_coordinates(pickup_node)
            record["pickup_lat"] = pickup_lat
            record["pickup_lon"] = pickup_lon
            dropoff_node = await graph_provider.nearest_node(
                record["dropoff_lat"], record["dropoff_lon"]
            )
            dropoff_lat, dropoff_lon = graph_provider.node_coordinates(dropoff_node)
            record["dropoff_lat"] = dropoff_lat
            record["dropoff_lon"] = dropoff_lon

            # Si el sorteo aleatorio coloca pickup y dropoff en el mismo
            # nodo (áreas urbanas densas), forzamos una separación mínima
            # re-muestreando el dropoff para que el pedido tenga sentido.
            attempts = 0
            while dropoff_node == pickup_node and attempts < 5:
                if FIXED_PICKUPS_ENABLED:
                    record["dropoff_lat"], record["dropoff_lon"] = self._random_point_near(
                        rng, record["pickup_lat"], record["pickup_lon"], record["dropoff_radius_km"]
                    )
                else:
                    record["dropoff_lat"], record["dropoff_lon"] = self._random_point_in_centro(rng)
                dropoff_node = await graph_provider.nearest_node(
                    record["dropoff_lat"], record["dropoff_lon"]
                )
                dropoff_lat, dropoff_lon = graph_provider.node_coordinates(dropoff_node)
                record["dropoff_lat"] = dropoff_lat
                record["dropoff_lon"] = dropoff_lon
                attempts += 1

            straight_line_km = _haversine_km(
                record["pickup_lat"],
                record["pickup_lon"],
                record["dropoff_lat"],
                record["dropoff_lon"],
            )
            base_points = round(
                BASE_POINTS + POINTS_PER_KM *
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
                base_points=base_points,
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
    def _random_point_in_centro(rng: random.Random) -> tuple[float, float]:
        """Punto aleatorio dentro de `CENTRO_MONTERREY_BOUNDS` -- el
        comportamiento ACTIVO por defecto (`FIXED_PICKUPS_ENABLED = False`),
        usado tanto para pickup como para dropoff."""
        min_lat, max_lat, min_lon, max_lon = CENTRO_MONTERREY_BOUNDS
        lat = rng.uniform(min_lat, max_lat)
        lon = rng.uniform(min_lon, max_lon)
        return lat, lon

    @staticmethod
    def _safe_dropoff_radius_km(pickup_lat: float, pickup_lon: float) -> float:
        """Radio máximo (km) en el que puede caer un dropoff alrededor de
        `pickup_lat/lon` sin arriesgarse a salir del grafo vial cargado.
        Los CEDIS cerca del borde del grafo (radio `CENTRO_MONTERREY_RADIUS_M`)
        obtienen un radio más chico que los que están bien adentro."""
        graph_radius_km = CENTRO_MONTERREY_RADIUS_M / 1000.0
        distance_from_center_km = _haversine_km(
            CENTRO_MONTERREY_LATITUDE, CENTRO_MONTERREY_LONGITUDE,
            pickup_lat, pickup_lon,
        )
        headroom_km = graph_radius_km - GRAPH_RADIUS_SAFETY_MARGIN_KM - distance_from_center_km
        return max(0.3, min(DROPOFF_RADIUS_KM, headroom_km))

    @staticmethod
    def _random_point_near(
        rng: random.Random, center_lat: float, center_lon: float, radius_km: float
    ) -> tuple[float, float]:
        """Punto aleatorio dentro de un círculo de `radius_km` alrededor de
        `center_lat/lon`, con densidad uniforme por área (no por radio --
        por eso la raíz cuadrada: sin ella, los puntos se amontonarían
        cerca del centro). Se usa para generar el dropoff de "última milla"
        de cada pedido relativo a su CEDIS de origen."""
        distance_km = radius_km * math.sqrt(rng.random())
        bearing_rad = rng.uniform(0, 2 * math.pi)
        angular_distance = distance_km / EARTH_RADIUS_KM

        lat_rad = math.radians(center_lat)
        lon_rad = math.radians(center_lon)
        new_lat_rad = math.asin(
            math.sin(lat_rad) * math.cos(angular_distance)
            + math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing_rad)
        )
        new_lon_rad = lon_rad + math.atan2(
            math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat_rad),
            math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat_rad),
        )
        return math.degrees(new_lat_rad), math.degrees(new_lon_rad)

    def _generate_solomon_style_records(
        self, rng: random.Random, num_orders: int
    ) -> List[dict]:
        """Genera registros con la misma semántica que un archivo Solomon
        VRPTW (customer_id, x, y, ready_time, due_date, service_time), pero
        con x/y sustituidos por coordenadas geográficas reales dentro del
        bounding box de Monterrey."""
        records: List[dict] = []
        for order_id in range(1, num_orders + 1):
            if FIXED_PICKUPS_ENABLED:
                pickup_location = rng.choice(FIXED_PICKUP_LOCATIONS)
                pickup_lat, pickup_lon = pickup_location["lat"], pickup_location["lon"]
                dropoff_radius_km = self._safe_dropoff_radius_km(pickup_lat, pickup_lon)
                dropoff_lat, dropoff_lon = self._random_point_near(
                    rng, pickup_lat, pickup_lon, dropoff_radius_km
                )
            else:
                pickup_lat, pickup_lon = self._random_point_in_centro(rng)
                dropoff_lat, dropoff_lon = self._random_point_in_centro(rng)
                dropoff_radius_km = None

            # Reparte los `ready_time_sec` de forma pareja a lo largo del
            # turno (con jitter chico), no uniforme al azar -- el azar puro
            # deja bolsas de varios pedidos cayendo en el mismo tick de 30s
            # con más frecuencia que un espaciado parejo, lo que fue la
            # causa real de los "se queda pensando" por contención bajo
            # ráfagas que se diagnosticó antes.
            #
            # El horizonte de liberación se recorta por
            # `MIN_TIME_WINDOW_SLACK_SEC` (no por un margen fijo chico):
            # así, sin importar qué tan tarde en el turno aparezca un
            # pedido, SIEMPRE le queda espacio para AL MENOS el slack
            # mínimo antes de que `due_time_sec = min(ready+slack,
            # shift_duration_sec)` lo recorte más abajo -- se usa el
            # mínimo, no el máximo, para no volver a juntar demasiados
            # pedidos al inicio del turno (la ráfaga que motivó espaciar
            # `ready_time_sec` parejo en primer lugar) en turnos cortos.
            # Con un margen fijo de 300s como antes, un pedido que
            # aparecía a 5 minutos del final del turno podía sortear un
            # slack de hasta 40 min y terminar con una ventana real de
            # apenas esos 5 minutos -- matemáticamente imposible de
            # cumplir, sin importar qué tan bien decida el agente.
            release_horizon_sec = max(
                self.shift_duration_sec - MIN_TIME_WINDOW_SLACK_SEC, 0
            )
            if num_orders == 1:
                ready_time_sec = 0
            else:
                release_position = (order_id - 1) / (num_orders - 1)
                jitter_sec = rng.randint(-30, 30)
                ready_time_sec = int(
                    max(0, min(
                        release_horizon_sec,
                        release_position * release_horizon_sec + jitter_sec,
                    ))
                )
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
                    "dropoff_radius_km": dropoff_radius_km,
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
