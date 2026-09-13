"""
risk_model.py
=============
Modelo de riesgo dinámico compartido, extraído de `risk_averse_agent.py`
para que **ambos** agentes midan el riesgo de una ruta con la misma vara.

Con aceptación obligatoria para los dos agentes (ninguno puede rechazar
una oferta), este módulo ya NO decide nada -- solo MIDE. Lo que sigue
diferenciando a Risk-Averse de Greedy es:
  1. `risk_bonus_points`: cuántos puntos extra gana cada entrega según qué
     tan bajo fue el riesgo de la ruta REAL recorrida (Risk-Averse, al
     elegir caminos Te+alpha*Re, tiende a ganar más de esto que Greedy,
     que siempre va por el camino más rápido a secas).
  2. `SAFETY_HARD_RISK_LIMIT`: ya no rechaza nada -- solo clasifica, para
     reporte, qué entregas tuvieron una exposición objetivamente alta
     (`risky_orders_handled` en agent_interface.py).
"""

from __future__ import annotations

import math
from typing import List, Tuple

from agent_interface import DriverState, Offer, ShiftState, WeatherEvent
from city_graph import CityGraphProvider

EARTH_RADIUS_KM = 6371.0

# Cuántos posibles "puntos de partida" se consideran al evaluar la pierna de
# recogida de una oferta nueva: la posición física actual, más hasta N-1
# dropoffs de pedidos ya aceptados. Acotado para no disparar el costo (cada
# candidato extra es un Dijkstra más) más allá del presupuesto de 200 ms.
MAX_PICKUP_ORIGIN_CANDIDATES = 3

# Radio en línea recta (barato de medir, sin Dijkstra) bajo el cual vale la
# pena pagar un Dijkstra EXTRA para ver si de verdad sale a cuenta recoger
# la oferta nueva desde el dropoff de un pedido ya aceptado. Filtrar por
# esto antes de consultar el grafo real es lo que mantiene el costo de
# `candidate_pickup_origins` casi nulo en el caso común (ningún dropoff
# cercano) -- sin el filtro, cada oferta pagaría hasta 2 Dijkstra extra
# SIEMPRE, lo que en pruebas reales empujó varias decisiones por encima del
# límite duro de 200 ms y las convirtió en timeouts (justo el efecto
# contrario al buscado: menos pedidos aceptados, no más).
PICKUP_PROXIMITY_KM = 2.5

# Umbral de "alto riesgo", compartido: un tramo con riesgo por encima de
# este valor se clasifica como riesgoso para REPORTE (`risky_orders_handled`
# en agent_interface.py) -- con aceptación obligatoria, ya no rechaza nada
# para ningún agente. Sigue siendo el mismo número para los dos, así la
# comparación ("cuántas entregas de Greedy vs. de Risk-Averse cruzaron este
# umbral") es una vara justa.
SAFETY_HARD_RISK_LIMIT = 0.75

# Bonus de Riesgo máximo (puntos) que otorga UNA entrega cuando su ruta
# real midió riesgo cero; se prorratea linealmente hasta 0 puntos a riesgo
# máximo (1.0). Es la pieza central del esquema de recompensas cerrado:
# con aceptación obligatoria para los dos agentes, esto -- no un rechazo --
# es lo único que puede premiar a Risk-Averse por elegir un camino más
# seguro para la MISMA entrega que Greedy también va a completar.
RISK_BONUS_MAX_POINTS = 20.0


def risk_bonus_points(weighted_risk: float) -> float:
    """Bonus de Riesgo (puntos) de una entrega dado el riesgo medido de la
    ruta real que se recorrió para cumplirla. Lineal y determinista -- sin
    esto, dos agentes que completan el mismo pedido por caminos distintos
    cobrarían lo mismo, y la elección de ruta más segura no tendría ningún
    reflejo en el marcador."""
    clamped_risk = max(0.0, min(weighted_risk, 1.0))
    return round(RISK_BONUS_MAX_POINTS * (1.0 - clamped_risk), 2)

# Puntos de Monterrey con antecedentes REALES de inundación/encharcamiento
# (coordenadas exactas, radio en km, severidad base en [0, 1]). Se usan para
# modular Re cuando hay lluvia.
#
# FUENTE: dataset oficial "Puntos de inundación" del municipio de Monterrey,
# publicado en su portal de datos abiertos geoespaciales (GeoNode/MIDE):
#   https://mide.monterrey.gob.mx/geoserver/ows?service=WFS&version=1.0.0
#     &request=GetFeature&typename=geonode:puntos_inundacion&outputFormat=csv
# Descargado y filtrado a los 34 puntos que caen dentro del radio de 6 km
# del grafo vial (ver CENTRO_MONTERREY_* en city_graph.py); nombres de
# intersección tal como los capturó el municipio (incluye erratas propias
# del dataset -- no se "corrigieron" para no fingir una fuente distinta).
# El radio y la severidad de cada punto SÍ son una estimación nuestra (el
# dataset municipal no trae esos dos campos) basada en el tipo de lugar:
# cruces de río/arroyo (0.9, más extendido), pasos a desnivel/distribuidores
# (0.8) e intersecciones de calle comunes (0.65) -- la EXISTENCIA y
# ubicación del punto de riesgo sí es dato real, no inventado.
FLOOD_PRONE_ZONES: Tuple[Tuple[str, float, float, float, float], ...] = (
    ('Puentes de Revolucion con Constitucion y Morones prieto', 25.672205, -100.286860, 0.6, 0.9),
    ('Puente Guadalupe y Constitucion', 25.680982, -100.273595, 0.6, 0.9),
    ('Lecho del rio Santa Catarina en colonia Del Carmen', 25.672568, -100.360644, 0.6, 0.9),
    ('Revolucion y Puente Soliradidad col. Rincon de la primavera', 25.646827, -100.274775, 0.6, 0.9),
    ('Revolucin y Lecho del Rio la Silla', 25.644611, -100.274382, 0.6, 0.9),
    ('Rio la Silla en col. El Pirul', 25.636449, -100.270443, 0.6, 0.9),
    ('Arroyo Seco en Valle del Mirador', 25.640098, -100.309423, 0.6, 0.9),
    ('Arroyo Seco en col. Altamira', 25.640618, -100.309980, 0.6, 0.9),
    ('Arroyo Seco en col. Canteras', 25.641103, -100.311583, 0.6, 0.9),
    ('Arroyo Seco en col. Las Retamas', 25.641968, -100.297490, 0.6, 0.9),
    ('Arroyo Seco y Rio Panuco col. Mexico', 25.642736, -100.291209, 0.6, 0.9),
    ('Arroyo Seco y Junco de la Vega col. Musas', 25.645448, -100.283180, 0.6, 0.9),
    ('Arroyo Seco y Lazaro Cardenas', 25.641204, -100.317093, 0.6, 0.9),
    ('Arroyo seco en col. Balcones de Altavista', 25.641896, -100.296491, 0.6, 0.9),
    ('Arroyo seco en col. Altamira (2)', 25.639146, -100.305512, 0.6, 0.9),
    ('Paso a desnivel de Pino Zuarez y Cuauhtemoc con Constitucion', 25.665474, -100.320069, 0.5, 0.8),
    ('Paso a desnivel de Zaragoza y Zuazua con Constitucion', 25.663746, -100.310960, 0.5, 0.8),
    ('Complejo vial Gonzalitos col. Ovispado', 25.676072, -100.350384, 0.5, 0.8),
    ('Bernardo Reyes y Alfonso Reyes col. fracc Bernardo Reyes', 25.721001, -100.330184, 0.4, 0.65),
    ('Gonzalitos y Madero col. Vista Hermosa', 25.687952, -100.351637, 0.4, 0.65),
    ('Gonzalitos y Lazaro Cardena col. Zapata', 25.698684, -100.351209, 0.4, 0.65),
    ('Puante Venustiano Carranza y Constitucion', 25.667789, -100.334328, 0.4, 0.65),
    ('Pas. a des. de Cons. y Moroes con Eugenio Garzazada/Felix U. gomez', 25.667752, -100.298343, 0.4, 0.65),
    ('Complejo Mira Valle', 25.671341, -100.368542, 0.4, 0.65),
    ('Av. Ruiz Cortinez y Camino a Santa Domingo col. Juana de Arco', 25.703807, -100.292856, 0.4, 0.65),
    ('23 de Abil y Maclovio Herrera col. Nueva Madero', 25.692434, -100.278273, 0.4, 0.65),
    ('23 de Abril y Via a Tampico col. Nueva Madero', 25.693935, -100.278518, 0.4, 0.65),
    ('Eugenio Garza Sada y Del Estado col. Tecnologico', 25.651467, -100.292396, 0.4, 0.65),
    ('Revolucion y Alfonzo Reyes col. Contry', 25.640393, -100.273109, 0.4, 0.65),
    ('Orion y Perseo col. Contry', 25.638836, -100.277360, 0.4, 0.65),
    ('Antonio I. Villarreal y Alfonzo Santos Palomo col. Coyoacan', 25.705213, -100.277428, 0.4, 0.65),
    ('Plan de Mipla y Plan de Paracuaro col. Republica', 25.627558, -100.296910, 0.4, 0.65),
    ('Manuel Barregan a la altura de Hogares FFCC col. Hidalgo', 25.708606, -100.316930, 0.4, 0.65),
    ('Quinta Zona y Lopez Hikman', 25.667554, -100.293981, 0.4, 0.65),
)

# Intersecciones REALES con más accidentes de tránsito registrados dentro
# del radio del grafo (nombre, lat, lon, radio_km, severidad, conteo real
# de accidentes -- este último solo informativo, no se usa en el cálculo).
#
# FUENTE: dataset oficial "Incidentes viales" del municipio de Monterrey
# (programa "Nuestras Calles Seguras", Dirección de Seguridad Vial de la
# Secretaría de Desarrollo Urbano), mismo portal MIDE/GeoNode:
#   https://mide.monterrey.gob.mx/geoserver/ows?service=WFS&version=1.0.0
#     &request=GetFeature&typename=geonode:incidentes_viales&outputFormat=csv
# 282,496 accidentes georreferenciados (2017-2024) dentro de los 6 km del
# grafo, agregados en celdas de ~600m y quedándonos con las 25 celdas con
# más accidentes; el nombre de cada una es la calle/cruce más frecuente
# reportado ahí. La severidad SÍ es derivada del conteo real (escalada
# linealmente al rango [0.55, 0.95] dentro de este top-25), no inventada.
# Cruza con el hallazgo independiente de OCISEVI (Observatorio Ciudadano de
# Seguridad Vial de Nuevo León, ocisevi.org.mx) de que Gonzalitos,
# Constitución, Colón, Venustiano Carranza, Garza Sada y Morones Prieto
# están entre las 15 avenidas con más siniestros de la metrópoli 2021-2023
# -- las mismas calles aparecen aquí de forma independiente.
#
# A diferencia de `FLOOD_PRONE_ZONES` (que solo eleva el riesgo cuando hay
# lluvia activa), esto se suma SIEMPRE en `point_risk`: son intersecciones
# peligrosas por diseño/tráfico, no por clima.
ACCIDENT_HOTSPOTS: Tuple[Tuple[str, float, float, float, float, int], ...] = (
    ('Constitucion / Gonzalitos', 25.673873, -100.352220, 0.35, 0.95, 8808),
    ('Colon / Villagran', 25.685289, -100.319908, 0.35, 0.91, 8250),
    ('Constitucion / Felix U Gomez', 25.667755, -100.297340, 0.35, 0.78, 6268),
    ('Gonzalitos / Madero', 25.686523, -100.351276, 0.35, 0.78, 6244),
    ('Constitucion / Cuauhtemoc', 25.667694, -100.319764, 0.35, 0.76, 5927),
    ('Cuauhtemoc / Colon', 25.685267, -100.314104, 0.35, 0.73, 5418),
    ('Gonzalitos / Pablo Gonzalez G.', 25.680688, -100.351710, 0.35, 0.71, 5236),
    ('Gonzalitos / Ruiz Cortines', 25.704383, -100.351232, 0.35, 0.69, 4929),
    ('Gonzalitos / Terranova', 25.693350, -100.351358, 0.35, 0.69, 4862),
    ('Constitucion / Venustiano Carranza', 25.667597, -100.333730, 0.35, 0.68, 4760),
    ('Gonzalitos / Leones', 25.697455, -100.350759, 0.35, 0.68, 4676),
    ('Revolucion / Chapultepec', 25.667679, -100.284042, 0.35, 0.67, 4537),
    ('Cuauhtemoc / Juan I Ramon', 25.673860, -100.319027, 0.35, 0.65, 4313),
    ('Venustiano Carranza / Colon', 25.686189, -100.331394, 0.35, 0.65, 4230),
    ('Madero / Felix U Gomez', 25.680761, -100.296835, 0.35, 0.63, 3974),
    ('Constitucion / Revolucion', 25.672476, -100.285638, 0.35, 0.63, 3908),
    ('Constitucion / Juarez', 25.662949, -100.313994, 0.35, 0.61, 3665),
    ('Felix U Gomez / Colon', 25.684973, -100.296342, 0.35, 0.60, 3550),
    ('Cuauhtemoc / Ruperto Martinez', 25.679807, -100.319308, 0.35, 0.58, 3238),
    ('Mm De Llano / Emilio Carranza', 25.679612, -100.313655, 0.35, 0.57, 3046),
    ('Madero / Simon Bolivar', 25.686326, -100.343982, 0.35, 0.55, 2819),
    ('Leones / Simon Bolivar', 25.696713, -100.343545, 0.35, 0.55, 2817),
    ('Garza Sada / 2 De Abril', 25.656099, -100.295272, 0.35, 0.55, 2794),
    ('Zaragoza / Espinosa', 25.680242, -100.308462, 0.35, 0.55, 2760),
    ('Morones Prieto / Cuauhtemoc', 25.662729, -100.319527, 0.35, 0.55, 2753),
)

# Cuánto pesa la cercanía a un ACCIDENT_HOTSPOT en el riesgo puntual, en la
# misma escala que el resto de `point_risk` (Re en [0,1]). No se necesita
# lluvia para que aplique -- a diferencia de las zonas de inundación, estas
# intersecciones son peligrosas por tráfico/diseño vial todo el año.
ACCIDENT_HOTSPOT_WEIGHT = 0.5


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en línea recta entre dos coordenadas. Se usa ÚNICAMENTE
    para medir la proximidad a zonas de riesgo, nunca para decidir rutas ni
    tiempos de viaje (eso siempre viene del grafo vial real)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _severity_for_event(shift_state: ShiftState, event: WeatherEvent) -> float:
    if event in shift_state.active_events:
        return shift_state.event_severity
    return 0.0


def point_risk(lat: float, lon: float, shift_state: ShiftState) -> float:
    """Riesgo puntual [0, 1] en una coordenada dada, bajo las condiciones
    ambientales actuales del turno (clima activo + proximidad a puntos
    reales de inundación) MÁS un riesgo base por tráfico que no depende del
    clima: estar cerca de una intersección con historial real de muchos
    accidentes (`ACCIDENT_HOTSPOTS`) ya es más peligroso un día despejado,
    no solo cuando llueve."""
    rain_severity = max(
        _severity_for_event(shift_state, WeatherEvent.HEAVY_RAIN),
        _severity_for_event(shift_state, WeatherEvent.FLASH_FLOOD),
    )
    traffic_severity = _severity_for_event(shift_state, WeatherEvent.HEAVY_TRAFFIC)
    heat_severity = _severity_for_event(shift_state, WeatherEvent.EXTREME_HEAT)

    risk = 0.05  # riesgo ambiental base (Monterrey, tráfico ordinario)
    risk += 0.35 * rain_severity
    risk += 0.15 * traffic_severity
    risk += 0.08 * heat_severity

    if rain_severity > 0.0:
        for _name, zone_lat, zone_lon, radius_km, zone_base_severity in FLOOD_PRONE_ZONES:
            distance_km = haversine_km(lat, lon, zone_lat, zone_lon)
            if distance_km <= radius_km:
                proximity_factor = 1.0 - (distance_km / radius_km)
                risk += proximity_factor * zone_base_severity * rain_severity

    # Siempre activo (no depende de `rain_severity`): una intersección con
    # miles de accidentes reales en el historial de Monterrey es riesgosa
    # todo el año, y más aún si además hay tráfico pesado activo.
    traffic_amplifier = 1.0 + traffic_severity
    for _name, zone_lat, zone_lon, radius_km, hotspot_severity, _count in ACCIDENT_HOTSPOTS:
        distance_km = haversine_km(lat, lon, zone_lat, zone_lon)
        if distance_km <= radius_km:
            proximity_factor = 1.0 - (distance_km / radius_km)
            risk += proximity_factor * hotspot_severity * ACCIDENT_HOTSPOT_WEIGHT * traffic_amplifier

    return max(0.0, min(risk, 1.0))


def average_risk_along_path(
    graph_provider: CityGraphProvider,
    path_nodes: Tuple[int, ...],
    shift_state: ShiftState,
) -> float:
    """Riesgo promedio muestreado sobre una ruta YA CALCULADA (lista de
    nodos), sin volver a correr Dijkstra. A diferencia de
    `route_risk_and_time` (que primero calcula el camino más rápido y LUEGO
    lo muestrea), esto mide el riesgo del camino físico exacto que un
    agente de verdad recorrió -- p. ej. el elegido por
    `RiskAverseAgent.risk_weighted_path`, que puede no ser el más rápido a
    secas. Puramente síncrona (solo lecturas de diccionario sobre el grafo
    ya cargado en memoria): segura de llamar desde el cierre determinista
    del turno o justo después de trazar una ruta, sin I/O ni bloqueo del
    event loop."""
    if not path_nodes:
        return 0.05
    graph = graph_provider.graph
    if len(path_nodes) > 12:
        step = max(1, len(path_nodes) // 12)
        sampled_nodes = path_nodes[::step]
    else:
        sampled_nodes = path_nodes

    point_risks: List[float] = [
        point_risk(graph.nodes[node]["y"], graph.nodes[node]["x"], shift_state)
        for node in sampled_nodes
    ]
    return sum(point_risks) / len(point_risks) if point_risks else 0.05


async def route_risk_and_time(
    graph_provider: CityGraphProvider,
    origin_node: int,
    dest_node: int,
    shift_state: ShiftState,
) -> Tuple[float, float]:
    """Calcula (riesgo_promedio, tiempo_de_viaje_sec) para el tramo real
    entre dos nodos, muestreando el riesgo puntual a lo largo de la ruta
    más rápida real (nunca en línea recta). Un solo Dijkstra (`shortest_path`);
    el tiempo se deriva sumando `travel_time_sec` sobre esa misma ruta en
    vez de correr un segundo Dijkstra independiente -- ver el comentario
    equivalente que tenía `risk_averse_agent.RiskAverseAgent._route_risk_and_time`
    antes de este refactor, sobre el presupuesto de 200 ms por decisión."""
    try:
        path_nodes = await graph_provider.shortest_path(origin_node, dest_node)
    except Exception:
        return 1.0, float("inf")

    travel_time_sec = await graph_provider.path_travel_time_sec(path_nodes)
    if not math.isfinite(travel_time_sec):
        return 1.0, float("inf")

    average_risk = average_risk_along_path(graph_provider, path_nodes, shift_state)
    return average_risk, travel_time_sec


def candidate_pickup_origins(driver_state: DriverState, offer: Offer) -> List[int]:
    """Nodos desde los que le puede convenir a un repartidor ir a recoger
    `offer`: su posición física actual, más los dropoffs de pedidos que YA
    trae aceptados y que, en línea recta, quedan razonablemente cerca del
    pickup de esta oferta (dentro de `PICKUP_PROXIMITY_KM`).

    Sin esto, una oferta cuyo pickup cae justo al lado de un dropoff que el
    repartidor ya trae en curso se evaluaba SIEMPRE desde donde está PARADO
    en este instante -- que puede ser del otro lado de la ciudad si apenas
    va empezando otro pedido -- y se rechazaba por "muy lejos" aunque, para
    cuando de verdad la vaya a recoger (después de completar lo que ya
    trae), le cueste casi nada por estar justo ahí.

    El filtro de proximidad en línea recta (gratis, sin Dijkstra) es
    deliberado: solo se paga el Dijkstra real de un candidato cuando ya hay
    indicio barato de que puede convenir, así el costo extra por oferta se
    queda en ~0 en el caso común (ningún dropoff activo cerca) en vez de
    sumar hasta 2 Dijkstra extra SIEMPRE -- eso fue justo lo que, medido en
    pruebas reales, empujó varias decisiones por encima del límite duro de
    200 ms y las convirtió en timeouts (el efecto contrario al buscado)."""
    origins = [driver_state.current_node]
    if driver_state.active_orders:
        nearby = [
            order for order in driver_state.active_orders
            if haversine_km(
                order.dropoff_lat, order.dropoff_lon,
                offer.pickup_lat, offer.pickup_lon,
            ) <= PICKUP_PROXIMITY_KM
        ]
        # Entre los cercanos, los que se entregarían más pronto son los
        # candidatos más relevantes (aproximación: el orden real de entrega
        # lo decide `plan_route`, carísimo de repetir aquí por cada oferta).
        nearby.sort(key=lambda o: o.due_time_sec)
        origins.extend(
            order.dropoff_node
            for order in nearby[: MAX_PICKUP_ORIGIN_CANDIDATES - 1]
        )

    seen: set[int] = set()
    unique_origins: List[int] = []
    for node in origins:
        if node not in seen:
            seen.add(node)
            unique_origins.append(node)
    return unique_origins


def weighted_route_risk(
    risk_to_pickup: float,
    time_to_pickup_sec: float,
    risk_delivery: float,
    time_delivery_sec: float,
) -> float:
    """Riesgo de un viaje completo (a recoger + a entregar), ponderado por
    cuánto tiempo se pasa en cada tramo -- un tramo largo y riesgoso pesa
    más que uno corto. Misma fórmula para ambos agentes, así el KPI de
    "riesgo promedio aceptado" es una comparación justa entre los dos."""
    total_time = time_to_pickup_sec + time_delivery_sec
    if total_time > 0 and math.isfinite(total_time):
        return (risk_to_pickup * time_to_pickup_sec + risk_delivery * time_delivery_sec) / max(total_time, 1.0)
    return max(risk_to_pickup, risk_delivery)
