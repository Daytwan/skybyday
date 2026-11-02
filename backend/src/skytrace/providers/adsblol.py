"""ADSB.lol v2 adapter. No internal retries, implicit fallbacks or HTTP redirects."""

import asyncio
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from skytrace.models import (
    AircraftObservation,
    Bearing,
    Latitude,
    Longitude,
    ProviderBatch,
    RejectionCount,
    ResponseMetadata,
    SourceAttribution,
    SourceName,
    TrackingArea,
)
from skytrace.providers.base import AircraftProvider, ProviderError, utc_now

ADSBLOL_SOURCE = SourceAttribution(
    name="adsblol",
    attribution="Aircraft data: ADSB.lol",
    url="https://www.adsb.lol/",
    license="ODbL-1.0",
    license_url="https://opendatacommons.org/licenses/odbl/1-0/",
)
Age = Annotated[float, Field(ge=0)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class WireEnvelope(WireModel):
    ac: list[object] = Field(max_length=10_000)
    now: int = Field(ge=0)
    ctime: int = Field(ge=0)
    ptime: int = Field(ge=0)
    total: int = Field(ge=0)
    msg: Literal["No error"]


class WireAircraft(WireModel):
    hex: str = Field(pattern=r"^~?[0-9a-fA-F]{6}$")
    type: str = Field(min_length=1, max_length=64)
    flight: str | None = Field(default=None, max_length=32)
    t: str | None = Field(default=None, max_length=32)
    lat: Latitude | None = None
    lon: Longitude | None = None
    alt_baro: float | Literal["ground"] | None = None
    alt_geom: float | None = None
    gs: float | None = Field(default=None, ge=0)
    track: Bearing | None = None
    true_heading: Bearing | None = None
    mag_heading: Bearing | None = None
    baro_rate: float | None = None
    geom_rate: float | None = None
    roll: float | None = Field(default=None, ge=-180, le=180)
    seen: Age | None = None
    seen_pos: Age | None = None
    mlat: list[str] = Field(default_factory=list, max_length=64)
    tisb: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("flight", "t")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


def _convert(value: float | None, factor: float) -> float | None:
    return value * factor if value is not None else None


def _observation(
    row: WireAircraft, timestamp: datetime, received_at: datetime, source: SourceName = "adsblol"
) -> AircraftObservation:
    if (row.lat is None) != (row.lon is None):
        raise ValueError("Partial coordinate pair")
    has_position = row.lat is not None and row.seen_pos is not None
    baro_altitude = row.alt_baro if isinstance(row.alt_baro, float) else None
    return AircraftObservation(
        icao24=row.hex.lower(),
        source=source,
        source_type=row.type,
        provider_timestamp=timestamp,
        received_at=received_at,
        position_observed_at=timestamp - timedelta(seconds=row.seen_pos) if has_position else None,
        last_message_at=timestamp - timedelta(seconds=row.seen) if row.seen is not None else None,
        callsign=row.flight,
        latitude_deg=row.lat if has_position else None,
        longitude_deg=row.lon if has_position else None,
        barometric_altitude_m=_convert(baro_altitude, 0.3048),
        geometric_altitude_m=_convert(row.alt_geom, 0.3048),
        geometric_altitude_reference="wgs84_ellipsoid" if row.alt_geom is not None else None,
        groundspeed_mps=_convert(row.gs, 1852 / 3600),
        ground_track_deg=row.track,
        true_heading_deg=row.true_heading,
        magnetic_heading_deg=row.mag_heading,
        barometric_vertical_rate_mps=_convert(row.baro_rate, 0.3048 / 60),
        geometric_vertical_rate_mps=_convert(row.geom_rate, 0.3048 / 60),
        roll_deg=row.roll,
        aircraft_type=row.t,
        on_ground=True if row.alt_baro == "ground" else None,
        mlat_fields=tuple(row.mlat),
        tisb_fields=tuple(row.tisb),
    )


def normalize_payload(
    payload: object,
    *,
    received_at: datetime,
    max_snapshot_age_seconds: float = 90,
    future_tolerance_seconds: float = 5,
    http: ResponseMetadata | None = None,
    source: SourceAttribution = ADSBLOL_SOURCE,
    display_name: str = "ADSB.lol",
) -> ProviderBatch:
    """Decode v2's millisecond epoch explicitly, independently of host receipt time."""
    if received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    received_at = received_at.astimezone(UTC)
    try:
        envelope = WireEnvelope.model_validate(payload)
        timestamp = datetime.fromtimestamp(envelope.now / 1000, UTC)
    except (ValidationError, ValueError, OverflowError, OSError) as error:
        raise ProviderError(f"Invalid {display_name} response envelope") from error
    if envelope.total != len(envelope.ac):
        raise ProviderError(f"{display_name} response count mismatch")
    age = (received_at - timestamp).total_seconds()
    if age > max_snapshot_age_seconds or age < -future_tolerance_seconds:
        raise ProviderError(f"{display_name} snapshot is stale or its timestamp is in the future")

    aircraft = []
    rejected = Counter()
    for item in envelope.ac:
        try:
            row = WireAircraft.model_validate(item)
            if row.hex.startswith("~"):
                rejected["non_icao_address"] += 1
                continue
            aircraft.append(_observation(row, timestamp, received_at, source.name))
        except (ValidationError, ValueError, OverflowError):
            rejected["invalid_record"] += 1
    if envelope.ac and not aircraft:
        raise ProviderError(f"No valid aircraft in non-empty {display_name} response")
    return ProviderBatch(
        source=source,
        provider_timestamp=timestamp,
        received_at=received_at,
        aircraft=tuple(aircraft),
        raw_record_count=envelope.total,
        rejected_record_count=sum(rejected.values()),
        rejections=tuple(
            RejectionCount(reason=reason, count=count) for reason, count in sorted(rejected.items())
        ),
        http=http,
    )


def parse_retry_after(value: str | None, received_at: datetime) -> float | None:
    if value is None:
        return None
    try:
        if value.strip().isascii() and value.strip().isdigit():
            delay = float(value)
        else:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            delay = max(0.0, (timestamp - received_at).total_seconds())
        return delay if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class CapturedResponse:
    metadata: ResponseMetadata
    body: bytes


class AdsbLolProvider(AircraftProvider):
    source = "adsblol"
    attribution = ADSBLOL_SOURCE
    display_name = "ADSB.lol"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = utc_now,
        max_response_bytes: int = 4 * 1024 * 1024,
        max_snapshot_age_seconds: float = 90,
    ) -> None:
        self.client = client
        self.clock = clock
        self.max_response_bytes = max_response_bytes
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.last_response: CapturedResponse | None = None

    def request_url(self, area: TrackingArea) -> str:
        return (
            f"https://api.adsb.lol/v2/point/{area.latitude_deg}/"
            f"{area.longitude_deg}/{area.radius_nm}"
        )

    async def fetch(self, area: TrackingArea) -> ProviderBatch:
        self.last_response = None
        url = self.request_url(area)
        try:
            async with asyncio.timeout(15):
                async with self.client.stream(
                    "GET",
                    url,
                    follow_redirects=False,
                    timeout=httpx.Timeout(10, connect=5),
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "SkybyDay/0.1 (local research)",
                    },
                ) as response:
                    metadata = ResponseMetadata(
                        requested_url=str(response.request.url),
                        status_code=response.status_code,
                        received_at=self.clock(),
                        server_date=response.headers.get("date"),
                        age=response.headers.get("age"),
                        retry_after=response.headers.get("retry-after"),
                        content_type=response.headers.get("content-type"),
                        cache_control=response.headers.get("cache-control"),
                    )
                    self.last_response = CapturedResponse(metadata, b"")
                    if response.status_code != 200:
                        raise ProviderError(
                            f"{self.display_name} returned HTTP {response.status_code}",
                            retryable=response.status_code == 429 or response.status_code >= 500,
                            retry_after_seconds=parse_retry_after(
                                metadata.retry_after, metadata.received_at
                            ),
                        )
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                        != "application/json"
                    ):
                        raise ProviderError(f"{self.display_name} returned a non-JSON response")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise ProviderError(
                                f"{self.display_name} response exceeds the byte limit"
                            )
                        body.extend(chunk)
                    metadata = metadata.model_copy(update={"received_at": self.clock()})
                    self.last_response = CapturedResponse(metadata, bytes(body))
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError) as error:
                        raise ProviderError(
                            f"{self.display_name} returned malformed JSON"
                        ) from error
                    return normalize_payload(
                        payload,
                        received_at=metadata.received_at,
                        http=metadata,
                        max_snapshot_age_seconds=self.max_snapshot_age_seconds,
                        source=self.attribution,
                        display_name=self.display_name,
                    )
        except (httpx.HTTPError, TimeoutError) as error:
            raise ProviderError(f"{self.display_name} request failed or timed out") from error
