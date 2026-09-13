"""
weather_service.py
===================
Clima REAL actual de Monterrey, vía la API pública de Open-Meteo
(https://open-meteo.com -- gratuita, sin API key, datos de modelos
meteorológicos oficiales tipo ECMWF/GFS/DWD, no inventados).

Se usa como condición ambiental INICIAL de cada turno: al arrancar una
sesión, se consulta el clima real de este instante en las coordenadas del
depósito y se aplica como punto de partida de `LiveEnvironment` -- si
ahora mismo está lloviendo de verdad en Monterrey, el turno arranca con
lluvia real, no despejado por default. Los botones de "Evento sorpresa"
en el panel siguen funcionando igual que antes por encima de esto: son
para que quien presenta dispare un evento dramático a mitad de turno bajo
demanda, no para simular el clima "de verdad" (eso es exactamente lo que
este módulo sí hace).

Si la API no responde a tiempo (o el entorno no tiene salida a internet --
ver el problema ya documentado con overpass-api.de en city_graph.py), el
turno arranca en CLEAR por seguridad: un turno no debe quedarse esperando
una llamada externa para poder empezar.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from agent_interface import WeatherEvent

logger = logging.getLogger("the_courier.weather_service")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SEC = 6.0

# Umbrales de clasificación (°C, mm) calibrados para Monterrey, no valores
# genéricos: veranos regios rutinariamente pasan de 35°C sin ser un evento
# extremo real, así que el piso de "calor extremo" se pone más alto que en
# un clima templado.
EXTREME_HEAT_ONSET_C = 38.0
EXTREME_HEAT_SEVERITY_SPAN_C = 8.0  # a 46°C, severidad = 1.0

# Códigos WMO (https://open-meteo.com/en/docs -- tabla "WMO Weather
# interpretation codes") agrupados por qué tan fuerte es la lluvia.
WMO_LIGHT_RAIN_CODES = {51, 53, 55, 56, 57, 61, 80}
WMO_HEAVY_RAIN_CODES = {63, 65, 66, 67, 81, 82}
WMO_STORM_CODES = {95, 96, 99}


@dataclass(frozen=True)
class WeatherSnapshot:
    """Clima real detectado para arrancar un turno. `event`/`severity` son
    lo que consume `LiveEnvironment.apply_event`; el resto es solo para
    mostrarle a quien ve la demo de dónde salió ese estado inicial."""

    event: WeatherEvent
    severity: float
    temperature_c: float
    precipitation_mm: float
    wind_speed_kph: float
    description: str
    source: str = "Open-Meteo (api.open-meteo.com)"


def _classify(temperature_c: float, precipitation_mm: float, weather_code: int) -> tuple[WeatherEvent, float, str]:
    """Traduce una lectura real de Open-Meteo a nuestro `WeatherEvent` +
    severidad [0,1]. Prioriza lluvia/tormenta sobre calor si ambos
    aplicaran a la vez (lluvia es lo que más le importa al modelo de
    riesgo -- ver risk_model.py)."""
    if weather_code in WMO_STORM_CODES or precipitation_mm >= 10.0:
        severity = min(1.0, 0.7 + precipitation_mm / 40.0)
        return WeatherEvent.FLASH_FLOOD, round(severity, 2), "Tormenta/lluvia muy fuerte"

    if weather_code in WMO_HEAVY_RAIN_CODES or precipitation_mm >= 2.5:
        severity = min(1.0, 0.5 + precipitation_mm / 20.0)
        return WeatherEvent.HEAVY_RAIN, round(severity, 2), "Lluvia moderada/fuerte"

    if weather_code in WMO_LIGHT_RAIN_CODES or precipitation_mm > 0.0:
        severity = min(1.0, 0.25 + precipitation_mm / 10.0)
        return WeatherEvent.HEAVY_RAIN, round(severity, 2), "Lluvia ligera"

    if temperature_c >= EXTREME_HEAT_ONSET_C:
        severity = min(1.0, (temperature_c - EXTREME_HEAT_ONSET_C) / EXTREME_HEAT_SEVERITY_SPAN_C)
        return WeatherEvent.EXTREME_HEAT, round(max(severity, 0.15), 2), "Calor extremo"

    return WeatherEvent.CLEAR, 0.0, "Despejado"


def _fetch_sync(lat: float, lon: float) -> WeatherSnapshot:
    response = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,precipitation,rain,weather_code,wind_speed_10m",
            "timezone": "America/Monterrey",
        },
        timeout=REQUEST_TIMEOUT_SEC,
    )
    response.raise_for_status()
    current = response.json()["current"]

    temperature_c = float(current["temperature_2m"])
    precipitation_mm = float(current["precipitation"])
    wind_speed_kph = float(current["wind_speed_10m"])
    weather_code = int(current["weather_code"])

    event, severity, description = _classify(temperature_c, precipitation_mm, weather_code)
    return WeatherSnapshot(
        event=event,
        severity=severity,
        temperature_c=temperature_c,
        precipitation_mm=precipitation_mm,
        wind_speed_kph=wind_speed_kph,
        description=description,
    )


async def fetch_current_weather(lat: float, lon: float) -> Optional[WeatherSnapshot]:
    """Clima real de este instante en `lat/lon`, o `None` si la API no
    respondió a tiempo -- nunca lanza, para que un turno jamás se quede
    esperando una llamada externa para poder arrancar."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_sync, lat, lon), timeout=REQUEST_TIMEOUT_SEC + 2.0
        )
    except Exception as exc:
        logger.warning(
            "No se pudo obtener el clima real de Open-Meteo (%s); el turno arranca en CLEAR.",
            exc,
        )
        return None
