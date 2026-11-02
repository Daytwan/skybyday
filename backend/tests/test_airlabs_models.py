"""Wholly artificial Stage A contract fixtures; no provider data or networking."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from skytrace.config import Settings
from skytrace.models import AircraftObservation
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.airlabs_models import (
    AirLabsContext,
    AirLabsFlightContextService,
    FlightContextBatch,
    FlightContextIntent,
    FlightContextRejection,
    admit_intent,
    normalize_callsign,
)
from skytrace.services.ingestion import IngestionService
from skytrace.services.runtime import TelemetryRuntime

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def artificial_snapshot(count=1):
    ingestion = IngestionService(
        SyntheticProvider(clock=lambda: NOW),
        Settings(provider="synthetic"),
        clock=lambda: NOW,
    )
    snapshot = TelemetryRuntime(ingestion).snapshot()
    planes = tuple(
        AircraftObservation(
            icao24=f"{index:06x}",
            source="adsblol",
            source_type="artificial",
            provider_timestamp=NOW,
            received_at=NOW,
            position_observed_at=NOW,
            callsign="ZZZ001",
            latitude_deg=10,
            longitude_deg=20,
        )
        for index in range(count)
    )
    return snapshot.model_copy(
        update={
            "sequence": 42,
            "data": snapshot.data.model_copy(update={"aircraft": planes}),
        }
    )


def artificial_intent(snapshot):
    return dict(
        schema_version=1,
        stream_id=snapshot.stream_id,
        sequence=41,
        source="adsblol",
        icao24="000000",
        callsign="ZZZ001",
        position_observed_at=NOW.isoformat(),
        view="live",
    )


def artificial_context():
    return dict(
        retrieved_at=NOW.isoformat(),
        signal_at=NOW.isoformat(),
        matched_position_at=NOW.isoformat(),
        flight_icao="ZZZ001",
        departure=dict(iata=None, icao="KZZZ", time_utc=(NOW - timedelta(hours=1)).isoformat()),
        arrival=dict(iata="ZZZ", icao=None, time_utc=(NOW + timedelta(hours=1)).isoformat()),
        expires_at=(NOW + timedelta(seconds=60)).isoformat(),
    )


def test_whole_400_denominator_disabled_and_selected_unavailable():
    snapshot = artificial_snapshot(400)
    service = AirLabsFlightContextService(clock=lambda: NOW)
    batch = service.project(snapshot)
    assert batch.tracked_aircraft == batch.counts.disabled == len(batch.contexts) == 400
    assert (
        batch.counts.matched == batch.two_endpoint_contexts == batch.confirmed_current_flights == 0
    )
    assert all(
        row.context is None and row.current_flight_confirmed is False for row in batch.contexts
    )
    assert FlightContextBatch.model_validate_json(batch.model_dump_json()) == batch
    selected = AirLabsFlightContextService("selected", clock=lambda: NOW).project(snapshot)
    assert selected.service_state == "unavailable"
    assert selected.counts.not_requested == 400
    assert selected.counts.unavailable == 0  # Global stop does not claim per-row attempts.


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(tracked_aircraft=399),
        lambda x: x["contexts"].append(x["contexts"][0]),
        lambda x: x["contexts"][1].update(icao24=x["contexts"][0]["icao24"]),
        lambda x: x["counts"].update(disabled=399),
        lambda x: x["counts"].pop("unknown"),
        lambda x: x["counts"].update(extra=0),
        lambda x: x.update(two_endpoint_contexts=1),
        lambda x: x.update(confirmed_current_flights=True),
        lambda x: x.update(confirmed_current_flights=1),
        lambda x: x["contexts"][0].update(state="invented"),
        lambda x: x["contexts"][0].update(reason="raw error text"),
        lambda x: x["contexts"][0].update(current_flight_confirmed=0),
        lambda x: x["contexts"][0].update(context=artificial_context()),
    ],
)
def test_batch_rejects_inconsistent_or_unsafe_contract(mutation):
    batch = AirLabsFlightContextService(clock=lambda: NOW).project(artificial_snapshot(400))
    payload = batch.model_dump(mode="json")
    mutation(payload)
    with pytest.raises(ValidationError):
        FlightContextBatch.model_validate_json(json.dumps(payload))


def test_valid_matched_context_companion_nulls_and_reconciled_cohort():
    batch = AirLabsFlightContextService(clock=lambda: NOW).project(artificial_snapshot(400))
    payload = batch.model_dump(mode="json")
    payload["contexts"][0].update(state="matched", reason="matched", context=artificial_context())
    payload["counts"].update(disabled=399, matched=1)
    payload["two_endpoint_contexts"] = 1
    matched = FlightContextBatch.model_validate_json(json.dumps(payload))
    assert matched.counts.matched == matched.two_endpoint_contexts == 1
    assert matched.confirmed_current_flights == 0
    payload["contexts"][0]["context"] = None
    with pytest.raises(ValidationError):
        FlightContextBatch.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", True),
        ("sequence", -1),
        ("sequence", 2**53),
        ("sequence", 1.5),
        ("sequence", "1"),
        ("sequence", float("nan")),
        ("schema_version", True),
        ("stream_id", ""),
        ("stream_id", "x" * 129),
        ("icao24", "ABC123"),
        ("callsign", " zzz001 "),
        ("callsign", "ZZ001"),
        ("position_observed_at", "2026-10-28T00:00:00"),
        ("position_observed_at", "2026-10-28T01:00:00+01:00"),
        ("position_observed_at", "1999-12-31T00:00:00Z"),
        ("position_observed_at", 1793145600),
        ("position_observed_at", float("inf")),
    ],
)
def test_intent_strict_bounded_fields(field, value):
    payload = artificial_intent(artificial_snapshot())
    payload[field] = value
    with pytest.raises(ValidationError):
        FlightContextIntent.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x["departure"].update(iata=None, icao=None),
        lambda x: x["arrival"].update(iata="bad"),
        lambda x: x.update(provider_leg_id="invented"),
        lambda x: x.update(current_flight_confirmed=True),
        lambda x: x.update(retrieved_at="2026-10-28T00:00:00+05:00"),
        lambda x: x.update(expires_at=NOW.isoformat()),
        lambda x: x["departure"].update(time_utc=x["arrival"]["time_utc"]),
    ],
)
def test_context_structure(mutate):
    payload = artificial_context()
    mutate(payload)
    with pytest.raises(ValidationError):
        AirLabsContext.model_validate_json(json.dumps(payload))


def test_admission_accepts_lower_sequence_and_older_fresh_time():
    snapshot = artificial_snapshot()
    payload = artificial_intent(snapshot)
    payload["position_observed_at"] = (NOW - timedelta(seconds=89)).isoformat()
    admit_intent(FlightContextIntent.model_validate_json(json.dumps(payload)), snapshot, NOW)
    assert normalize_callsign(" \tzzz001A\r\n") == "ZZZ001A"
    assert normalize_callsign("\u00a0ZZZ001") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"stream_id": "other"},
        {"sequence": 43},
        {"source": "synthetic"},
        {"icao24": "abc123"},
        {"callsign": "ZZZ002"},
        {"view": "replay"},
        {"position_observed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"position_observed_at": (NOW - timedelta(seconds=91)).isoformat()},
    ],
)
def test_admission_rejects_future_or_changed_view_identity(changes):
    snapshot = artificial_snapshot()
    payload = artificial_intent(snapshot) | changes
    if changes.get("source") == "synthetic":
        with pytest.raises(ValidationError):
            FlightContextIntent.model_validate_json(json.dumps(payload))
        return
    with pytest.raises(FlightContextRejection) as caught:
        admit_intent(FlightContextIntent.model_validate_json(json.dumps(payload)), snapshot, NOW)
    assert caught.value.status == 409


@pytest.mark.parametrize(
    "updates,reason",
    [
        (
            {"position_observed_at": None, "latitude_deg": None, "longitude_deg": None},
            "missing_position",
        ),
        ({"position_observed_at": NOW - timedelta(seconds=91)}, "stale_position"),
        ({"latitude_deg": float("nan")}, "missing_position"),
        ({"callsign": "N12345"}, "invalid_callsign"),
    ],
)
def test_current_server_ineligibility(updates, reason):
    snapshot = artificial_snapshot()
    plane = snapshot.data.aircraft[0].model_copy(update=updates)
    snapshot = snapshot.model_copy(
        update={"data": snapshot.data.model_copy(update={"aircraft": (plane,)})}
    )
    intent = FlightContextIntent.model_validate_json(json.dumps(artificial_intent(snapshot)))
    with pytest.raises(FlightContextRejection) as caught:
        admit_intent(intent, snapshot, NOW)
    assert (caught.value.status, caught.value.reason) == (422, reason)


@pytest.mark.parametrize(
    "changes", [{"source": "synthetic"}, {"callsign": "ZZZ002"}, {"callsign": None}]
)
def test_matched_row_requires_live_source_and_raw_callsign_identity(changes):
    from skytrace.services.airlabs_models import ContextRow

    batch = AirLabsFlightContextService(clock=lambda: NOW).project(artificial_snapshot())
    row = batch.contexts[0].model_dump(mode="json")
    row.update(state="matched", reason="matched", context=artificial_context())
    row.update(changes)
    with pytest.raises(ValidationError):
        ContextRow.model_validate_json(json.dumps(row))
