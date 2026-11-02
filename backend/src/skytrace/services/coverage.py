"""Finite, source-supported ADSB.lol point-query coverage plans.

These plans are sampling schedules, not territorial coverage or global feeds.
"""

from dataclasses import dataclass
from typing import Literal

from skytrace.models import TrackingArea

CoveragePreset = Literal["den", "world_hubs_v1"]


@dataclass(frozen=True)
class CoverageArea:
    identifier: str
    name: str
    area: TrackingArea


@dataclass(frozen=True)
class CoveragePlan:
    preset: CoveragePreset
    kind: Literal["single_area", "sampled_hubs"]
    areas: tuple[CoverageArea, ...]

    @property
    def cycle_slots(self) -> int:
        return len(self.areas)


WORLD_HUBS_V1 = (
    CoverageArea(
        "den",
        "Denver (DEN)",
        TrackingArea(latitude_deg=39.8561, longitude_deg=-104.6737, radius_nm=60),
    ),
    CoverageArea(
        "lhr",
        "London Heathrow (LHR)",
        TrackingArea(latitude_deg=51.4700, longitude_deg=-0.4543, radius_nm=120),
    ),
    CoverageArea(
        "hnd",
        "Tokyo Haneda (HND)",
        TrackingArea(latitude_deg=35.5494, longitude_deg=139.7798, radius_nm=120),
    ),
    CoverageArea(
        "syd",
        "Sydney (SYD)",
        TrackingArea(latitude_deg=-33.9399, longitude_deg=151.1753, radius_nm=120),
    ),
    CoverageArea(
        "dxb",
        "Dubai (DXB)",
        TrackingArea(latitude_deg=25.2532, longitude_deg=55.3657, radius_nm=120),
    ),
    CoverageArea(
        "gru",
        "Sao Paulo Guarulhos (GRU)",
        TrackingArea(latitude_deg=-23.4356, longitude_deg=-46.4731, radius_nm=120),
    ),
)


def coverage_plan(preset: CoveragePreset, *, default_area: TrackingArea) -> CoveragePlan:
    if preset == "den":
        is_denver = (
            abs(default_area.latitude_deg - 39.8561) < 0.01
            and abs(default_area.longitude_deg + 104.6737) < 0.01
        )
        return CoveragePlan(
            preset="den",
            kind="single_area",
            areas=(CoverageArea(
                "den" if is_denver else "configured",
                "Denver (DEN)" if is_denver else "Configured area",
                default_area,
            ),),
        )
    return CoveragePlan(preset="world_hubs_v1", kind="sampled_hubs", areas=WORLD_HUBS_V1)


def coverage_area_count(preset: CoveragePreset) -> int:
    return 1 if preset == "den" else len(WORLD_HUBS_V1)
