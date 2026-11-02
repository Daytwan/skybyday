"""Offline audit of saved schema-v1 telemetry and standing-reference captures.

Deliberately independent of application imports, provider startup and network access.
Only validates fields needed for the audit; it is not a full API schema validator.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROVENANCE = ("source", "source_url", "source_snapshot_commit", "source_snapshot_retrieved_at")
OBSERVATION = ("provider_timestamp", "received_at", "position_observed_at", "last_message_at")
CATEGORIES = ("two_endpoint_references", "multi_leg_references", "unknown", "unavailable")


class AuditError(ValueError):
    """Capture pair cannot support this audit."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def obj(value, label):
    require(isinstance(value, dict), f"{label}: expected object")
    return value


def count(value, label):
    require(type(value) is int and value >= 0, f"{label}: expected nonnegative integer")
    return value


def timestamp(value, label, nullable=False):
    if value is None and nullable:
        return
    require(isinstance(value, str), f"{label}: expected timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError(f"{label}: invalid timestamp") from exc
    require(parsed.tzinfo is not None, f"{label}: timezone required")


def index(rows, label):
    require(isinstance(rows, list), f"{label}: expected array")
    result = {}
    for row in rows:
        obj(row, label)
        identifier = row.get("icao24")
        require(
            isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{6}", identifier),
            f"{label}: invalid icao24",
        )
        require(identifier not in result, f"{label}: duplicate ID {identifier}")
        result[identifier] = row
    return result


def json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise AuditError(f"nonfinite JSON number: {value}")


def finite_float(value):
    parsed = float(value)
    require(math.isfinite(parsed), f"nonfinite JSON number: {value}")
    return parsed


def read_capture(path):
    raw = Path(path).read_bytes()
    try:
        data = json.loads(
            raw,
            object_pairs_hook=json_pairs,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"invalid JSON: {path}") from exc
    return obj(data, str(path)), {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def audit(aircraft_path, routes_path):
    aircraft, aircraft_input = read_capture(aircraft_path)
    routes, routes_input = read_capture(routes_path)
    for label, capture in (("aircraft", aircraft), ("routes", routes)):
        require(
            type(capture.get("schema_version")) is int and capture["schema_version"] == 1,
            f"{label}: unsupported schema_version",
        )
        require(
            isinstance(capture.get("stream_id"), str) and capture["stream_id"],
            f"{label}: missing stream_id",
        )
        count(capture.get("sequence"), f"{label}.sequence")
    require(aircraft["stream_id"] == routes["stream_id"], "stream mismatch")
    require(aircraft["sequence"] == routes["sequence"], "sequence mismatch")
    require(aircraft.get("type") == "snapshot", "expected aircraft snapshot")
    data = obj(aircraft.get("data"), "aircraft.data")
    planes = index(data.get("aircraft"), "aircraft")
    references = index(routes.get("references"), "references")
    require(planes.keys() == references.keys(), "exact aircraft ID sets differ")
    timestamp(data.get("emitted_at"), "aircraft.emitted_at")
    timestamp(routes.get("emitted_at"), "routes.emitted_at")
    require(type(routes.get("source_ready")) is bool, "source_ready: expected boolean")
    totals = dict.fromkeys(CATEGORIES, 0)
    reasons = Counter()
    missing_counts = Counter()
    missing_observation_counts = Counter()
    rows = []
    for identifier in sorted(planes):
        plane = planes[identifier]
        route = obj(references[identifier].get("route"), f"route {identifier}")
        require("callsign" in plane and "callsign" in route, "missing callsign field")
        callsign = plane["callsign"]
        require(callsign is None or isinstance(callsign, str), "invalid callsign type")
        require(callsign == route["callsign"], f"callsign mismatch: {identifier}")
        require(
            route.get("current_flight_confirmed") is False,
            f"standing reference current_flight_confirmed must be false: {identifier}",
        )
        lookup = route.get("lookup_callsign")
        normalized = callsign.strip().upper() if callsign else None
        match = re.fullmatch(r"([A-Z]{3})([0-9][A-Z0-9]*)", normalized or "")
        if match:
            number = match[2].lstrip("0")
            if not number or number[0].isalpha():
                number = "0" + number
            normalized = match[1] + number
        if lookup is not None:
            require(
                isinstance(lookup, str)
                and re.fullmatch(r"[A-Z]{3}[A-Z0-9]{1,5}", lookup)
                and lookup == normalized,
                f"lookup callsign mismatch: {identifier}",
            )
        airports = route.get("airports")
        require(isinstance(airports, list) and len(airports) <= 12, "invalid airports array")
        for airport in airports:
            obj(airport, "airport")
            require(
                isinstance(airport.get("icao"), str)
                and re.fullmatch(r"[A-Z0-9]{3,8}", airport["icao"]),
                "invalid airport ID",
            )
            lat, lon = airport.get("latitude"), airport.get("longitude")
            require((lat is None) == (lon is None), "unpaired airport coordinates")
            if lat is not None:
                require(
                    all(type(v) in (float, int) and math.isfinite(v) for v in (lat, lon))
                    and -90 <= lat <= 90
                    and -180 <= lon <= 180,
                    "invalid airport coordinates",
                )
        status, reason = route.get("status"), route.get("reason")
        if status == "reference":
            require(lookup is not None and len(airports) >= 2, "incomplete reference")
            category = "two_endpoint_references" if len(airports) == 2 else "multi_leg_references"
            require(
                reason == ("available" if len(airports) == 2 else "multi_leg_reference"),
                "reference reason/airport count mismatch",
            )
        elif status == "unknown":
            require(
                not airports and reason in (None, "invalid_callsign", "unmatched"),
                "unknown route contradicts reason/airports",
            )
            require(reason != "unmatched" or lookup is not None, "unmatched requires lookup")
            require(reason != "invalid_callsign" or lookup is None, "invalid callsign has lookup")
            category = "unknown"
        elif status == "unavailable":
            require(
                not airports
                and reason in ("cooldown", "rate_limited", "source_error", "malformed"),
                "unavailable route contradicts reason/airports",
            )
            category = "unavailable"
        else:
            raise AuditError(f"unsupported route status: {status}")
        totals[category] += 1
        reasons[reason or "unspecified"] += 1
        for field in PROVENANCE:
            value = route.get(field)
            require(value is None or isinstance(value, str), f"invalid provenance {field}")
        missing = [field for field in PROVENANCE if route.get(field) in (None, "")]
        missing_counts.update(missing)
        missing_observation = [field for field in OBSERVATION if plane.get(field) is None]
        missing_observation_counts.update(missing_observation)
        for field in ("source_snapshot_retrieved_at", "fetched_at"):
            timestamp(route.get(field), f"route.{field}", nullable=True)
        for field in OBSERVATION:
            timestamp(plane.get(field), f"aircraft.{field}", nullable=True)
        rows.append(
            {
                "icao24": identifier,
                "callsign": callsign,
                "lookup_callsign": lookup,
                "category": category,
                "status": status,
                "reason": reason,
                "airport_ids": [airport["icao"] for airport in airports],
                "current_flight_confirmed": False,
                "observation_times": {field: plane.get(field) for field in OBSERVATION},
                "route_provenance": {
                    field: route.get(field) for field in (*PROVENANCE, "fetched_at")
                },
                "missing_provenance": missing,
                "missing_observation_times": missing_observation,
                "telemetry_source": plane.get("source"),
            }
        )
    totals.update(tracked_aircraft=len(planes), confirmed_current_flights=0)
    for field, actual in totals.items():
        require(count(routes.get(field), f"routes.{field}") == actual, f"declared {field} mismatch")
    require(
        sum(totals[field] for field in CATEGORIES) == len(planes), "cohort reconciliation failed"
    )
    for field in (
        "collected_aircraft_count",
        "published_aircraft_count",
        "truncated_aircraft_count",
        "aircraft_capacity",
    ):
        count(data.get(field), field)
    require(data["published_aircraft_count"] == len(planes), "published count mismatch")
    require(
        data["collected_aircraft_count"] == len(planes) + data["truncated_aircraft_count"],
        "omission counts mismatch",
    )
    require(len(planes) <= data["aircraft_capacity"], "published count exceeds capacity")
    tracking = obj(aircraft.get("tracking"), "tracking")
    coverage = data.get("coverage")
    require(isinstance(coverage, list), "coverage: expected array")
    require(
        count(tracking.get("coverage_area_count"), "coverage_area_count") == len(coverage),
        "coverage area count mismatch",
    )
    scope_ids = []
    for scope in coverage:
        obj(scope, "scope")
        require(isinstance(scope.get("id"), str) and scope["id"], "scope ID missing")
        require(scope["id"] not in scope_ids, "duplicate scope ID")
        scope_ids.append(scope["id"])
        require(type(scope.get("stale")) is bool, "scope stale: expected boolean")
        timestamp(scope.get("last_success_at"), "scope.last_success_at", nullable=True)
        age = scope.get("freshness_age_seconds")
        require(
            age is None or (type(age) in (float, int) and math.isfinite(age) and age >= 0),
            "invalid scope freshness",
        )
    return {
        "audit_schema_version": 1,
        "inputs": {"aircraft": aircraft_input, "routes": routes_input},
        "stream_id": aircraft["stream_id"],
        "sequence": aircraft["sequence"],
        "capture_times": {
            "aircraft_emitted_at": data["emitted_at"],
            "routes_emitted_at": routes["emitted_at"],
            "filesystem_time_used": False,
        },
        "counts": totals,
        "reason_counts": dict(sorted(reasons.items())),
        "aircraft_ids": sorted(planes),
        "rows": rows,
        "provenance": {
            "fields": list(PROVENANCE),
            "missing_field_counts": dict(sorted(missing_counts.items())),
            "complete_rows": sum(not row["missing_provenance"] for row in rows),
            "missing_observation_time_counts": dict(sorted(missing_observation_counts.items())),
            "meaning": "Field presence only; not independent truth or current-flight accuracy.",
        },
        "traffic": {
            "tracking": tracking,
            "source": data.get("source"),
            "source_ready": routes["source_ready"],
            "batch": data.get("batch"),
            "coverage": sorted(coverage, key=lambda scope: scope["id"]),
            "active_scope_id": data.get("active_scope_id"),
            "omissions": {
                field: data[field]
                for field in (
                    "collected_aircraft_count",
                    "published_aircraft_count",
                    "truncated_aircraft_count",
                    "aircraft_capacity",
                )
            },
        },
        "limits": (
            "Frozen saved cohort; sampled regions are not worldwide coverage. "
            "Standing references are not confirmed flights. No aircraft taxonomy inferred."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aircraft", type=Path)
    parser.add_argument("routes", type=Path)
    parser.add_argument(
        "--output", type=Path, help="New evidence file; refuses overwrite. Defaults to stdout."
    )
    args = parser.parse_args()
    try:
        result = (
            json.dumps(audit(args.aircraft, args.routes), sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        )
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(result)
        else:
            sys.stdout.write(result)
    except (AuditError, OSError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
