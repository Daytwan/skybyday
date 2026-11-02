"""Source-specific public contract and inert Stage A service; no acquisition code."""

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, field_validator, model_validator

from skytrace.models import AircraftObservation, FrozenModel, SourceName
from skytrace.services.runtime import SnapshotMessage

ContextState = Literal[
    "disabled",
    "ineligible",
    "not_requested",
    "queued",
    "pending",
    "matched",
    "unknown",
    "unavailable",
]
ContextReason = Literal[
    "disabled",
    "unsupported_source",
    "invalid_callsign",
    "missing_position",
    "stale_position",
    "not_requested",
    "queued",
    "pending",
    "matched",
    "no_result",
    "identity_mismatch",
    "status_unsupported",
    "missing_fields",
    "invalid_fields",
    "stale_signal",
    "time_mismatch",
    "invalidated",
    "queue_expired",
    "queue_full",
    "capacity",
    "cooldown",
    "local_budget",
    "allocation_exhausted",
    "allocation_expired",
    "rate_limited",
    "authentication",
    "entitlement",
    "month_exhausted",
    "clock_untrusted",
    "ledger_unavailable",
    "source_error",
    "malformed",
    "body_too_large",
    "timeout",
    "cancelled",
    "identity_changed",
    "invalid_request",
    "origin_rejected",
    "lifecycle_unavailable",
]
Mode = Literal["off", "selected"]
SafeSequence = Annotated[int, Field(ge=0, le=2**53 - 1)]
Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{6}$")]
Callsign = Annotated[str, Field(pattern=r"^[A-Z]{3}[0-9][A-Z0-9]{0,4}$")]
Stream = Annotated[str, Field(min_length=1, max_length=128)]
Retry = Annotated[float, Field(ge=0)]


def utc_timestamp(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0) or not 2000 <= value.year <= 2100:
        raise ValueError("Timestamp must be UTC within years 2000-2100")
    return value


UtcTimestamp = Annotated[AwareDatetime, AfterValidator(utc_timestamp)]


class ContractModel(FrozenModel):
    @model_validator(mode="before")
    @classmethod
    def exact_constants(cls, value):
        # Literal equality alone treats True as 1 and 0 as False in Python.
        if isinstance(value, dict):
            for name, expected in (
                ("schema_version", 1),
                ("current_flight_confirmed", False),
                ("confirmed_current_flights", 0),
            ):
                if name in value and type(value[name]) is not type(expected):
                    raise ValueError("Invalid constant type")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def no_numeric_timestamp(cls, value, info):
        if info.field_name.endswith(("_at", "_utc")) and value is not None:
            if isinstance(value, str):
                return datetime.fromisoformat(value)
            if not isinstance(value, datetime):
                raise ValueError("Timestamp must be an ISO UTC timestamp")
        return value


class AirLabsEndpoint(ContractModel):
    iata: str | None = Field(pattern=r"^[A-Z]{3}$")
    icao: str | None = Field(pattern=r"^[A-Z0-9]{4}$")
    time_utc: UtcTimestamp

    @model_validator(mode="after")
    def usable_code(self) -> Self:
        if self.iata is None and self.icao is None:
            raise ValueError("An endpoint needs a usable code")
        return self


class AirLabsContext(ContractModel):
    source: Literal["airlabs"] = "airlabs"
    source_url: Literal["https://airlabs.co/docs/flight"] = "https://airlabs.co/docs/flight"
    retrieved_at: UtcTimestamp
    signal_at: UtcTimestamp
    matched_position_at: UtcTimestamp
    flight_icao: Callsign
    provider_leg_id: None = None
    identity_basis: Literal["aircraft_callsign_time_window"] = "aircraft_callsign_time_window"
    departure: AirLabsEndpoint
    arrival: AirLabsEndpoint
    endpoint_basis: Literal["provider_reported"] = "provider_reported"
    time_basis: Literal["provider_reported_unix_unspecified_operation"] = (
        "provider_reported_unix_unspecified_operation"
    )
    expires_at: UtcTimestamp
    current_flight_confirmed: Literal[False] = False

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if not self.departure.time_utc < self.arrival.time_utc:
            raise ValueError("Departure must precede arrival")
        if not self.retrieved_at < self.expires_at <= self.arrival.time_utc:
            raise ValueError("Context expiry must follow retrieval and not exceed arrival")
        return self


class FlightContextIntent(ContractModel):
    schema_version: Literal[1]
    stream_id: Stream
    sequence: SafeSequence
    source: Literal["adsblol"]
    icao24: Hex
    callsign: Callsign
    position_observed_at: UtcTimestamp
    view: str = Field(min_length=1, max_length=32)


class ContextRow(ContractModel):
    icao24: Hex
    source: SourceName
    callsign: str | None = Field(max_length=32)
    lookup_callsign: Callsign | None
    position_observed_at: UtcTimestamp | None
    state: ContextState
    reason: ContextReason | None
    retry_in_seconds: Retry | None = None
    current_flight_confirmed: Literal[False] = False
    context: AirLabsContext | None = None

    @model_validator(mode="after")
    def matched_payload_only(self) -> Self:
        if (self.state == "matched") != (self.context is not None):
            raise ValueError("Only matched rows require a context")
        if self.context is not None and (
            self.source != "adsblol"
            or self.lookup_callsign != normalize_callsign(self.callsign)
            or self.context.flight_icao != self.lookup_callsign
        ):
            raise ValueError("Context must match the live source and row callsign")
        return self


class ContextCounts(ContractModel):
    disabled: int = Field(ge=0, le=10_000)
    ineligible: int = Field(ge=0, le=10_000)
    not_requested: int = Field(ge=0, le=10_000)
    queued: int = Field(ge=0, le=10_000)
    pending: int = Field(ge=0, le=10_000)
    matched: int = Field(ge=0, le=10_000)
    unknown: int = Field(ge=0, le=10_000)
    unavailable: int = Field(ge=0, le=10_000)


class FlightContextBatch(ContractModel):
    schema_version: Literal[1] = 1
    stream_id: Stream
    sequence: SafeSequence
    emitted_at: UtcTimestamp
    mode: Mode
    service_state: Literal["disabled", "ready", "cooldown", "suspended", "unavailable"]
    service_reason: ContextReason | None
    tracked_aircraft: int = Field(ge=0, le=10_000)
    counts: ContextCounts
    two_endpoint_contexts: int = Field(ge=0, le=10_000)
    confirmed_current_flights: Literal[0] = 0
    contexts: list[ContextRow] = Field(max_length=10_000)

    @model_validator(mode="after")
    def whole_cohort(self) -> Self:
        actual = {name: 0 for name in ContextCounts.model_fields}
        for row in self.contexts:
            actual[row.state] += 1
        if (
            self.tracked_aircraft != len(self.contexts)
            or len({row.icao24 for row in self.contexts}) != len(self.contexts)
            or self.counts.model_dump() != actual
            or self.two_endpoint_contexts != actual["matched"]
        ):
            raise ValueError("Context membership and counts must reconcile")
        return self


class FlightContextCommand(ContractModel):
    schema_version: Literal[1] = 1
    stream_id: Stream | None = None
    sequence: SafeSequence | None = None
    icao24: Hex | None = None
    callsign: Callsign | None = None
    state: ContextState
    reason: ContextReason | None
    retry_in_seconds: Retry | None = None
    context: AirLabsContext | None = None

    @model_validator(mode="after")
    def matched_payload_only(self) -> Self:
        if (self.state == "matched") != (self.context is not None):
            raise ValueError("Only matched commands require a context")
        if self.context is not None and self.context.flight_icao != self.callsign:
            raise ValueError("Context callsign must match the command")
        return self


class FlightContextRejection(Exception):
    """Safe typed boundary error: never retain exception text or request content."""

    def __init__(self, status: int, reason: ContextReason) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason)


def normalize_callsign(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip(" \t\r\n\v\f").translate(
        str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    )
    return normalized if re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9]{0,4}", normalized) else None


def ineligible_reason(
    plane: AircraftObservation,
    snapshot: SnapshotMessage,
    now: datetime,
) -> ContextReason | None:
    if plane.source != "adsblol":
        return "unsupported_source"
    if normalize_callsign(plane.callsign) is None:
        return "invalid_callsign"
    if (
        plane.position_observed_at is None
        or plane.latitude_deg is None
        or plane.longitude_deg is None
    ):
        return "missing_position"
    if not (
        math.isfinite(plane.latitude_deg)
        and -90 <= plane.latitude_deg <= 90
        and math.isfinite(plane.longitude_deg)
        and -180 <= plane.longitude_deg <= 180
    ):
        return "missing_position"
    age = (now - plane.position_observed_at).total_seconds()
    if not 0 <= age <= min(snapshot.tracking.stale_after_seconds, 90):
        return "stale_position"
    return None


def admit_intent(intent: FlightContextIntent, snapshot: SnapshotMessage, now: datetime) -> None:
    """Input checks precede global service gating; client time is only a hint."""
    if intent.view != "live":
        raise FlightContextRejection(409, "identity_changed")
    plane = next((row for row in snapshot.data.aircraft if row.icao24 == intent.icao24), None)
    if (
        plane is None
        or intent.stream_id != snapshot.stream_id
        or intent.sequence > snapshot.sequence
        or intent.source != plane.source
    ):
        raise FlightContextRejection(409, "identity_changed")
    reason = ineligible_reason(plane, snapshot, now)
    if reason is not None:
        raise FlightContextRejection(422, reason)
    if (
        intent.callsign != normalize_callsign(plane.callsign)
        or intent.position_observed_at > plane.position_observed_at
        or intent.position_observed_at > now
    ):
        raise FlightContextRejection(409, "identity_changed")
    if (now - intent.position_observed_at).total_seconds() > min(
        snapshot.tracking.stale_after_seconds, 90
    ):
        raise FlightContextRejection(409, "identity_changed")


class AirLabsFlightContextService:
    """Inert implementation and explicit injection seam for later reviewed stages.

    Even selected configuration cannot activate this implementation. No client,
    worker, queue, ledger, file access, or result cache exists in Stage A.
    """

    def __init__(
        self,
        mode: Mode = "off",
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.mode = mode
        self.clock = clock

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def observe_snapshot(self, message: SnapshotMessage) -> None:
        pass

    def submit(
        self, intent: FlightContextIntent, snapshot: SnapshotMessage
    ) -> FlightContextCommand:
        raise FlightContextRejection(
            503, "disabled" if self.mode == "off" else "lifecycle_unavailable"
        )

    def project(self, snapshot: SnapshotMessage) -> FlightContextBatch:
        rows = []
        now = self.clock()
        for plane in snapshot.data.aircraft:
            reason = ineligible_reason(plane, snapshot, now)
            state: ContextState
            if self.mode == "off":
                state, reason = "disabled", "disabled"
            elif reason is not None:
                state = "ineligible"
            else:
                state, reason = "not_requested", "not_requested"
            rows.append(
                ContextRow(
                    icao24=plane.icao24,
                    source=plane.source,
                    callsign=plane.callsign,
                    lookup_callsign=normalize_callsign(plane.callsign),
                    position_observed_at=plane.position_observed_at,
                    state=state,
                    reason=reason,
                )
            )
        counts = {name: 0 for name in ContextCounts.model_fields}
        for row in rows:
            counts[row.state] += 1
        return FlightContextBatch(
            stream_id=snapshot.stream_id,
            sequence=snapshot.sequence,
            emitted_at=now,
            mode=self.mode,
            service_state="disabled" if self.mode == "off" else "unavailable",
            service_reason="disabled" if self.mode == "off" else "lifecycle_unavailable",
            tracked_aircraft=len(rows),
            counts=ContextCounts(**counts),
            two_endpoint_contexts=0,
            contexts=rows,
        )
