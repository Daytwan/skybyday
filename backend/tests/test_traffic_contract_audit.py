"""Artificial small capture pairs plus verification of the preserved historical pairs."""

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend/scripts/audit_traffic_contract.py"
spec = importlib.util.spec_from_file_location("traffic_audit", SCRIPT)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)
FIXTURES = Path(__file__).parent / "fixtures/traffic-contract"
HISTORICAL = ROOT / "data/qa/skybyday-world-2026-10-24"


@pytest.fixture
def pair():
    return tuple(
        json.loads((FIXTURES / name).read_text()) for name in ("aircraft.json", "routes.json")
    )


def write_pair(tmp_path, pair):
    paths = [tmp_path / "aircraft.json", tmp_path / "routes.json"]
    for path, value in zip(paths, pair, strict=True):
        path.write_text(json.dumps(value))
    return paths


def test_all_categories_remain_in_denominator_and_provenance_is_explicit(tmp_path, pair):
    result = audit_module.audit(*write_pair(tmp_path, pair))
    assert result["counts"] == dict(
        tracked_aircraft=5,
        two_endpoint_references=1,
        multi_leg_references=1,
        unknown=2,
        unavailable=1,
        confirmed_current_flights=0,
    )
    assert len(result["rows"]) == len(result["aircraft_ids"]) == 5
    assert result["aircraft_ids"] == sorted(result["aircraft_ids"])
    assert result["provenance"]["missing_field_counts"]["source_snapshot_commit"] == 5
    assert result["traffic"]["omissions"]["truncated_aircraft_count"] == 1
    assert (
        result["rows"][0]["observation_times"]["received_at"]
        != result["capture_times"]["aircraft_emitted_at"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "stream",
        "sequence",
        "bool_sequence",
        "schema",
        "ids",
        "duplicate_plane",
        "duplicate_route",
        "tracked",
        "category_count",
        "confirmed",
        "missing_confirmed",
        "callsign",
        "lookup",
        "reference_airports",
        "reason",
        "status",
        "coordinates",
        "published",
        "omissions",
        "capacity",
        "scope_count",
        "scope_duplicate",
        "timestamp",
        "missing_data",
        "bad_rows",
        "bad_route",
    ],
)
def test_rejects_malformed_or_mismatched_pairs(tmp_path, pair, mutation):
    aircraft, routes = deepcopy(pair)
    route = routes["references"][0]["route"]
    if mutation == "stream":
        routes["stream_id"] = "another-stream"
    elif mutation == "sequence":
        routes["sequence"] += 1
    elif mutation == "bool_sequence":
        aircraft["sequence"] = routes["sequence"] = True
    elif mutation == "schema":
        routes["schema_version"] = 2
    elif mutation == "ids":
        routes["references"][0]["icao24"] = "ffffff"
    elif mutation == "duplicate_plane":
        aircraft["data"]["aircraft"].append(aircraft["data"]["aircraft"][0])
    elif mutation == "duplicate_route":
        routes["references"].append(routes["references"][0])
    elif mutation == "tracked":
        routes["tracked_aircraft"] += 1
    elif mutation == "category_count":
        routes["unknown"] += 1
    elif mutation == "confirmed":
        route["current_flight_confirmed"] = True
    elif mutation == "missing_confirmed":
        del route["current_flight_confirmed"]
    elif mutation == "callsign":
        route["callsign"] = "OTHER1"
    elif mutation == "lookup":
        route["lookup_callsign"] = "OTHER1"
    elif mutation == "reference_airports":
        route["airports"] = []
    elif mutation == "reason":
        route["reason"] = "unmatched"
    elif mutation == "status":
        route["status"] = "invented"
    elif mutation == "coordinates":
        route["airports"][0]["latitude"] = 200
    elif mutation == "published":
        aircraft["data"]["published_aircraft_count"] = 0
    elif mutation == "omissions":
        aircraft["data"]["truncated_aircraft_count"] = 0
    elif mutation == "capacity":
        aircraft["data"]["aircraft_capacity"] = 1
    elif mutation == "scope_count":
        aircraft["tracking"]["coverage_area_count"] = 0
    elif mutation == "scope_duplicate":
        aircraft["data"]["coverage"] *= 2
        aircraft["tracking"]["coverage_area_count"] = 2
    elif mutation == "timestamp":
        aircraft["data"]["emitted_at"] = "yesterday"
    elif mutation == "missing_data":
        del aircraft["data"]
    elif mutation == "bad_rows":
        routes["references"] = {}
    elif mutation == "bad_route":
        routes["references"][0]["route"] = []
    with pytest.raises(audit_module.AuditError):
        audit_module.audit(*write_pair(tmp_path, (aircraft, routes)))


@pytest.mark.parametrize(
    "raw", [b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"[]", b"\xff"]
)
def test_rejects_invalid_json(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(audit_module.AuditError):
        audit_module.read_capture(path)


def test_cli_is_deterministic_and_refuses_to_overwrite(tmp_path, pair):
    paths = write_pair(tmp_path, pair)
    command = [sys.executable, str(SCRIPT), *map(str, paths)]
    first = subprocess.run(command, capture_output=True, check=True)
    second = subprocess.run(command, capture_output=True, check=True)
    assert first.stdout == second.stdout
    output = tmp_path / "evidence.json"
    subprocess.run([*command, "--output", str(output)], check=True)
    assert output.read_bytes() == first.stdout
    failure = subprocess.run([*command, "--output", str(output)], capture_output=True)
    assert failure.returncode == 2
    assert output.read_bytes() == first.stdout
    paths[1].write_text("{}")
    invalid = subprocess.run(command, capture_output=True)
    assert invalid.returncode == 2 and not invalid.stdout


@pytest.mark.parametrize(
    "suffix,counts,reasons",
    [
        ("", (400, 315, 4, 81), (54, 27)),
        ("-second", (392, 250, 15, 127), (101, 26)),
    ],
)
def test_preserved_historical_cohorts(suffix, counts, reasons):
    paths = (
        HISTORICAL / f"aircraft{suffix}-capture.json",
        HISTORICAL / f"routes{suffix}-capture.json",
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        pytest.skip("Local-only historical audit inputs unavailable: " + ", ".join(missing))
    result = audit_module.audit(*paths)
    assert (
        tuple(
            result["counts"][field]
            for field in (
                "tracked_aircraft",
                "two_endpoint_references",
                "multi_leg_references",
                "unknown",
            )
        )
        == counts
    )
    assert result["counts"]["unavailable"] == result["counts"]["confirmed_current_flights"] == 0
    assert (
        tuple(result["reason_counts"][field] for field in ("invalid_callsign", "unmatched"))
        == reasons
    )


def test_empty_cohort_reconciles_without_invented_rows(tmp_path, pair):
    aircraft, routes = pair
    aircraft["data"]["aircraft"] = []
    for field in (
        "collected_aircraft_count",
        "published_aircraft_count",
        "truncated_aircraft_count",
    ):
        aircraft["data"][field] = 0
    routes["references"] = []
    for field in ("tracked_aircraft", "two_endpoint_references", "multi_leg_references", "unknown"):
        routes[field] = 0
    routes["unavailable"] = 0
    result = audit_module.audit(*write_pair(tmp_path, pair))
    assert result["rows"] == result["aircraft_ids"] == []
    assert all(value == 0 for value in result["counts"].values())


def test_missing_local_historical_inputs_are_explicitly_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HISTORICAL", tmp_path)
    with pytest.raises(
        pytest.skip.Exception, match="Local-only historical audit inputs unavailable"
    ):
        test_preserved_historical_cohorts("", (), ())
