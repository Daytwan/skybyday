"""Deterministic offline observations; never presented as actual aircraft."""

from collections.abc import Callable
from datetime import UTC, datetime
from math import asin, atan2, cos, degrees, pi, radians, sin

from skytrace.models import AircraftObservation, ProviderBatch, SourceAttribution, TrackingArea
from skytrace.providers.base import AircraftProvider, utc_now

SYNTHETIC_SOURCE = SourceAttribution(
    name="synthetic",
    attribution="Synthetic SkyTrace test data; not real aircraft",
    url=None,
    license="MIT",
    license_url=None,
)


class SyntheticProvider(AircraftProvider):
    source = "synthetic"

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock

    async def fetch(self, area: TrackingArea) -> ProviderBatch:
        now = self.clock().astimezone(UTC)
        # Explicitly fictional paths for repeatable viewer updates. This is a
        # spherical display fixture, not a flight dynamics or navigation model.
        phase = (now.timestamp() % 1200) / 1200 * 2 * pi
        radius = min(area.radius_nm * 1852 * 0.2, 20_000)
        positions = (
            _destination(area, radius * 0.6, phase),
            _destination(area, radius, phase + pi),
        )
        # A one-second forward difference of each fictional path provides a
        # repeatable ground-track cue. It is not body heading, pitch or roll.
        next_phase = phase + 2 * pi / 1200
        tracks = (
            _bearing(positions[0], _destination(area, radius * 0.6, next_phase)),
            _bearing(positions[1], _destination(area, radius, next_phase + pi)),
        )
        common = dict(
            source="synthetic",
            source_type="synthetic",
            provider_timestamp=now,
            received_at=now,
            position_observed_at=now,
            last_message_at=now,
        )
        return ProviderBatch(
            source=SYNTHETIC_SOURCE,
            provider_timestamp=now,
            received_at=now,
            raw_record_count=2,
            rejected_record_count=0,
            aircraft=(
                AircraftObservation(
                    **common,
                    icao24="000001",
                    callsign="SYNTH01",
                    latitude_deg=positions[0][0],
                    longitude_deg=positions[0][1],
                    ground_track_deg=tracks[0],
                    geometric_altitude_m=3000.0,
                    geometric_altitude_reference="wgs84_ellipsoid",
                ),
                AircraftObservation(
                    **common,
                    icao24="000002",
                    callsign="SYNTH02",
                    latitude_deg=positions[1][0],
                    longitude_deg=positions[1][1],
                    ground_track_deg=tracks[1],
                ),
            ),
        )


def _destination(area: TrackingArea, distance_m: float, bearing: float) -> tuple[float, float]:
    latitude, longitude = radians(area.latitude_deg), radians(area.longitude_deg)
    angle = distance_m / 6_371_008.8
    target_latitude = asin(sin(latitude) * cos(angle) + cos(latitude) * sin(angle) * cos(bearing))
    target_longitude = longitude + atan2(
        sin(bearing) * sin(angle) * cos(latitude),
        cos(angle) - sin(latitude) * sin(target_latitude),
    )
    return degrees(target_latitude), (degrees(target_longitude) + 180) % 360 - 180


def _bearing(start: tuple[float, float], end: tuple[float, float]) -> float:
    lat1, lon1 = map(radians, start)
    lat2, lon2 = map(radians, end)
    return (
        degrees(
            atan2(
                sin(lon2 - lon1) * cos(lat2),
                cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(lon2 - lon1),
            )
        )
        % 360
    )
