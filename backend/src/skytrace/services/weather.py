"""One shared, bounded NWS airport-observation cache. No synthetic weather."""

import asyncio
import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from skytrace.providers.adsblol import parse_retry_after

URL = "https://api.weather.gov/stations/KDEN/observations/latest"


class WeatherObservation(BaseModel):
    station: Literal["KDEN"] = "KDEN"
    observed_at: AwareDatetime
    description: str = Field(max_length=200)
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    wind_direction_deg: float | None = None
    wind_speed_mps: float | None = None
    wind_gust_mps: float | None = None
    visibility_m: float | None = None
    cloud_cover: str | None = None
    cloud_base_m: float | None = None


class WeatherResponse(BaseModel):
    source: Literal["NWS"] = "NWS"
    source_url: str = URL
    status: Literal["available", "unavailable", "degraded"]
    observation: WeatherObservation | None
    fetched_at: AwareDatetime | None
    stale: bool
    retry_in_seconds: int
    message: str


def quantity(properties: dict, key: str, unit: str, factor: float = 1) -> float | None:
    value = properties.get(key)
    if not isinstance(value, dict) or value.get("unitCode") != f"wmoUnit:{unit}":
        return None
    number = value.get("value")
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
    ):
        return None
    quality = value.get("qualityControl")
    if quality is not None and (not isinstance(quality, str) or quality in {"X", "Z"}):
        return None
    return number * factor


def normalize_weather(value: object, now: datetime) -> WeatherObservation:
    if not isinstance(value, dict) or not isinstance(value.get("properties"), dict):
        raise ValueError("Invalid observation")
    p = value["properties"]
    if p.get("stationId") != "KDEN":
        raise ValueError("Wrong weather station")
    layers = p.get("cloudLayers")
    layer = layers[0] if isinstance(layers, list) and layers and isinstance(layers[0], dict) else {}
    cover = layer.get("amount")
    if not isinstance(cover, str):
        cover = None
    result = WeatherObservation(
        observed_at=p.get("timestamp"),
        description=p.get("textDescription") or "Conditions not reported",
        temperature_c=quantity(p, "temperature", "degC"),
        dewpoint_c=quantity(p, "dewpoint", "degC"),
        wind_direction_deg=quantity(p, "windDirection", "degree_(angle)"),
        wind_speed_mps=quantity(p, "windSpeed", "km_h-1", 1 / 3.6),
        wind_gust_mps=quantity(p, "windGust", "km_h-1", 1 / 3.6),
        visibility_m=quantity(p, "visibility", "m"),
        cloud_cover=cover if cover in {"CLR", "SKC", "FEW", "SCT", "BKN", "OVC", "VV"} else None,
        cloud_base_m=None if cover in {"CLR", "SKC"} else quantity(layer, "base", "m"),
    )
    if result.observed_at > now + timedelta(minutes=1):
        raise ValueError("Future observation")
    for key in ("wind_speed_mps", "wind_gust_mps", "visibility_m", "cloud_base_m"):
        number = getattr(result, key)
        if number is not None and number < 0:
            setattr(result, key, None)
    if result.wind_direction_deg is not None and not 0 <= result.wind_direction_deg <= 360:
        result.wind_direction_deg = None
    return result


class WeatherService:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.lock = asyncio.Lock()
        self.next_fetch = 0.0
        self.observation: WeatherObservation | None = None
        self.fetched_at: datetime | None = None
        self.failed = False
        self.failures = 0

    async def get(self) -> WeatherResponse:
        async with self.lock:
            if time.monotonic() >= self.next_fetch:
                # Reserve before I/O so cancellation cannot cause rapid upstream retries.
                self.next_fetch = time.monotonic() + 600
                self.failed = True
                try:
                    async with (
                        asyncio.timeout(10),
                        self.client.stream(
                            "GET",
                            URL,
                            timeout=8,
                            headers={
                                "User-Agent": "SkybyDay/0.1 (self-hosted aviation explorer)",
                                "Accept": "application/geo+json",
                            },
                        ) as response,
                    ):
                        delay = parse_retry_after(
                            response.headers.get("retry-after"), datetime.now(UTC)
                        )
                        if delay is not None:
                            self.next_fetch = max(self.next_fetch, time.monotonic() + delay)
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 262_144:
                                raise ValueError("Weather response too large")
                        self.observation = normalize_weather(json.loads(content), datetime.now(UTC))
                    self.fetched_at = datetime.now(UTC)
                    self.failed = False
                    self.failures = 0
                except (httpx.HTTPError, TimeoutError, ValueError, ValidationError):
                    self.failures = min(self.failures + 1, 4)
                    delay = min(3600, 600 * 2 ** (self.failures - 1)) + random.uniform(0, 60)
                    self.next_fetch = max(self.next_fetch, time.monotonic() + delay)
        return WeatherResponse(
            status="degraded"
            if self.failed and self.observation
            else "unavailable"
            if self.failed
            else "available",
            observation=self.observation,
            fetched_at=self.fetched_at,
            stale=self.observation is None
            or (datetime.now(UTC) - self.observation.observed_at).total_seconds() > 5400,
            retry_in_seconds=math.ceil(max(0, self.next_fetch - time.monotonic())),
            message="Weather service unavailable; cached observations may be old."
            if self.failed
            else "Latest reported KDEN observation; station reports can be delayed.",
        )
