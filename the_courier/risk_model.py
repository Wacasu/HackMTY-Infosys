"""
risk_model.py
=============
Modelo de riesgo dinámico compartido, extraído de `risk_averse_agent.py`
para que **ambos** agentes puedan medir el riesgo de una ruta con la misma
vara -- el Agente Inteligente lo usa para decidir (Costo = Te + alpha*Re);
el Agente Base lo usa ÚNICAMENTE para reportar en el panel de KPIs qué tan
riesgosos fueron los viajes que aceptó, sin que afecte su lógica de
aceptación (que sigue ignorando el riesgo por diseño, alpha=0).

Esta separación es lo que permite justificar el modelo con datos reales:
sin ella, no hay forma de saber "qué tanto riesgo aceptó el Greedy" porque
el Greedy nunca lo calculaba.
"""

from __future__ import annotations

import math
from typing import List, Tuple

from agent_interface import ShiftState, WeatherEvent
from city_graph import CityGraphProvider

EARTH_RADIUS_KM = 6371.0

# Umbral duro de seguridad, compartido: un tramo con riesgo por encima de
# este valor se considera "de alto riesgo" sin importar cuánto pague.
# `RiskAverseAgent` lo usa para RECHAZAR de verdad; `GreedyAgent` solo lo
# usa para REPORTAR (nunca decide con esto -- sigue ciego al riesgo por
# diseño). Esa comparación ("Greedy aceptó N pedidos que superan este
# umbral; Risk-averse rechazó M de ellos") es el KPI central para
# justificar el modelo: cuántos incidentes potenciales evita de verdad.
SAFETY_HARD_RISK_LIMIT = 0.75

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
    ambientales actuales del turno (clima activo + proximidad a zonas de
    Monterrey propensas a inundación)."""
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

    return max(0.0, min(risk, 1.0))


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
    average_risk = sum(point_risks) / len(point_risks) if point_risks else 0.05
    return average_risk, travel_time_sec


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
