from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from skytrace.models import AircraftObservation


def test_canonical_model_rejects_naive_times_incomplete_positions_and_nonfinite_numbers() -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    base = dict(
        icao24="00a123",
        source="adsblol",
        source_type="adsb_icao",
        provider_timestamp=now,
        received_at=now,
    )
    for update in [
        {"received_at": now.replace(tzinfo=None)},
        {"latitude_deg": 39.0},
        {"groundspeed_mps": float("nan")},
        {"true_heading_deg": 360.0},
        {"icao24": "~123456"},
        {"geometric_altitude_m": 1000.0},
    ]:
        with pytest.raises(ValidationError):
            AircraftObservation(**(base | update))
