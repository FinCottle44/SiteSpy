"""OpenWeather integration for SiteSpy — fetch current weather for a site.

Uses OpenWeather Current Weather API (2.5) to attach weather conditions
to each timelapse snapshot at ingest time. Fails open: if the call fails,
returns None and the snapshot proceeds without weather data.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)

_OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
_TIMEOUT_SECONDS = 3  # aggressive timeout — don't slow ingest


@dataclass(frozen=True)
class WeatherSnapshot:
    """Weather data captured at ingest time."""

    condition: str  # e.g. "Rain", "Clear", "Clouds"
    description: str  # e.g. "light rain", "clear sky"
    temp_c: float  # temperature in Celsius
    feels_like_c: float
    humidity_pct: int  # 0–100
    wind_speed_ms: float  # wind speed in m/s
    wind_deg: int  # wind direction in degrees
    visibility_m: int  # visibility in metres
    cloud_pct: int  # cloudiness 0–100


def fetch_current_weather(lat: float, lon: float) -> WeatherSnapshot | None:
    """Fetch current weather from OpenWeather for the given coordinates.

    Returns None if:
    - OPENWEATHER_API_KEY env var is not set
    - The API call fails or times out
    - The response is malformed

    This function is intentionally fire-and-forget safe.
    """
    api_key = os.environ.get("OPENWEATHER_API_KEY", "")
    if not api_key:
        logger.debug("OPENWEATHER_API_KEY not set, skipping weather fetch")
        return None

    url = (
        f"{_OPENWEATHER_BASE_URL}"
        f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    )

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))

        weather_block = data.get("weather", [{}])[0]
        main_block = data.get("main", {})
        wind_block = data.get("wind", {})

        return WeatherSnapshot(
            condition=weather_block.get("main", "Unknown"),
            description=weather_block.get("description", ""),
            temp_c=round(float(main_block.get("temp", 0)), 1),
            feels_like_c=round(float(main_block.get("feels_like", 0)), 1),
            humidity_pct=int(main_block.get("humidity", 0)),
            wind_speed_ms=round(float(wind_block.get("speed", 0)), 1),
            wind_deg=int(wind_block.get("deg", 0)),
            visibility_m=int(data.get("visibility", 0)),
            cloud_pct=int(data.get("clouds", {}).get("all", 0)),
        )

    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("weather_fetch_network_error", extra={"error": str(exc)})
        return None
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        logger.warning("weather_fetch_parse_error", extra={"error": str(exc)})
        return None


def weather_to_dynamo_map(weather: WeatherSnapshot) -> dict[str, dict[str, str]]:
    """Convert a WeatherSnapshot to a DynamoDB M (map) attribute value."""
    return {
        "M": {
            "condition": {"S": weather.condition},
            "description": {"S": weather.description},
            "temp_c": {"N": str(weather.temp_c)},
            "feels_like_c": {"N": str(weather.feels_like_c)},
            "humidity_pct": {"N": str(weather.humidity_pct)},
            "wind_speed_ms": {"N": str(weather.wind_speed_ms)},
            "wind_deg": {"N": str(weather.wind_deg)},
            "visibility_m": {"N": str(weather.visibility_m)},
            "cloud_pct": {"N": str(weather.cloud_pct)},
        }
    }
