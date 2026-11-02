"""One bounded AirLabs attempt. No discovery, retries, telemetry or persistence."""

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from pydantic import SecretStr

from skytrace.services.airlabs_models import AirLabsEndpoint, ContextReason, normalize_callsign

URL = "https://airlabs.co/api/v9/flight"
BODY_LIMIT = 256 * 1024
DEADLINE_SECONDS = 20
_private_request = ContextVar("airlabs_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX's normal INFO message includes the query credential. Context-local
        # filtering also covers httpcore DEBUG headers/errors without hiding other tasks.
        return not _private_request.get()


_log_filter = _PrivateRequestFilter()


@dataclass(frozen=True)
class ProviderFlight:
    hex: str
    flight_icao: str
    signal_at: datetime
    departure: AirLabsEndpoint
    arrival: AirLabsEndpoint

    @property
    def local_leg(self) -> tuple[str, str, datetime, datetime]:
        return self.hex, self.flight_icao, self.departure.time_utc, self.arrival.time_utc


@dataclass(frozen=True)
class LookupResult:
    flight: ProviderFlight | None = None
    reason: ContextReason | None = None
    retry_after_seconds: int | float | None = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite number")


def retry_after_seconds(value: str | None, now: datetime) -> int | float:
    """Preserve large valid deltas; never shorten a provider cooldown to a cap."""
    if value is not None:
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            try:
                return max(60, int(value))
            except ValueError:
                # Beyond Python's integer-string safety bound: fail closed indefinitely.
                return math.inf
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(60, (date - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 60


def _unix(value) -> datetime:
    if type(value) is not int:
        raise ValueError("Invalid UNIX time")
    result = datetime.fromtimestamp(value, UTC)
    if not 2000 <= result.year <= 2100:
        raise ValueError("Invalid UNIX range")
    return result


def _endpoint(row: dict, prefix: str) -> AirLabsEndpoint:
    codes = {}
    for kind in ("iata", "icao"):
        code = row.get(f"{prefix}_{kind}")
        if code is not None:
            if not isinstance(code, str) or not code.isascii():
                raise ValueError("Invalid airport code")
            code = code.upper()
        codes[kind] = code
    return AirLabsEndpoint(**codes, time_utc=_unix(row.get(f"{prefix}_time_ts")))


def _api_error(error) -> ContextReason:
    # Only closed, explicitly recognized codes affect persistent stop policy.
    # Unknown errors remain source_error; provider messages are always discarded.
    code = error.get("code") if isinstance(error, dict) else error
    if not isinstance(code, str):
        return "source_error"
    if code in {"unknown_api_key", "invalid_api_key", "expired_api_key"}:
        return "authentication"
    if code in {"minute_limit_exceeded", "hour_limit_exceeded", "rate_limit_exceeded"}:
        return "rate_limited"
    if code in {"month_limit_exceeded", "monthly_limit_exceeded"}:
        return "month_exhausted"
    if code in {"access_denied", "endpoint_access_denied", "subscription_required"}:
        return "entitlement"
    return "source_error"


def normalize_response(body: bytes) -> LookupResult:
    """Discard the envelope and all unallowlisted attributes before returning."""
    try:
        envelope = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, TypeError, RecursionError):
        return LookupResult(reason="malformed")
    try:
        if not isinstance(envelope, dict):
            return LookupResult(reason="malformed")
        if "error" in envelope:
            return LookupResult(reason=_api_error(envelope["error"]))
        row = envelope.get("response")
        if row is None:
            return LookupResult(reason="no_result")
        if not isinstance(row, dict):
            return LookupResult(reason="invalid_fields")
        if row.get("status") != "en-route":
            return LookupResult(reason="status_unsupported")
        if any(
            row.get(field) is None
            for field in ("hex", "flight_icao", "updated", "dep_time_ts", "arr_time_ts")
        ):
            return LookupResult(reason="missing_fields")
        hex_value = row["hex"]
        callsign_value = row["flight_icao"]
        if not isinstance(hex_value, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
            return LookupResult(reason="invalid_fields")
        callsign = normalize_callsign(callsign_value) if isinstance(callsign_value, str) else None
        if callsign is None:
            return LookupResult(reason="invalid_fields")
        flight = ProviderFlight(
            hex=hex_value.lower(),
            flight_icao=callsign,
            signal_at=_unix(row["updated"]),
            departure=_endpoint(row, "dep"),
            arrival=_endpoint(row, "arr"),
        )
        if flight.departure.time_utc >= flight.arrival.time_utc:
            return LookupResult(reason="time_mismatch")
        return LookupResult(flight=flight)
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        return LookupResult(reason="invalid_fields")


class AirLabsClient:
    def __init__(
        self,
        key: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._key = key
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=DEADLINE_SECONDS,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
        for name in ("httpx", *tuple(logging.Logger.manager.loggerDict)):
            if name == "httpx" or name.startswith("httpcore."):
                logging.getLogger(name).addFilter(_log_filter)

    async def close(self) -> None:
        token = _private_request.set(True)
        try:
            await self._client.aclose()
        finally:
            self._client.cookies.clear()
            _private_request.reset(token)

    async def lookup(self, flight_icao: str) -> LookupResult:
        if normalize_callsign(flight_icao) != flight_icao:
            return LookupResult(reason="invalid_callsign")
        token = _private_request.set(True)
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                async with self._client.stream(
                    "GET",
                    URL,
                    params={"api_key": self._key.get_secret_value(), "flight_icao": flight_icao},
                ) as response:
                    now = self._clock()
                    if response.status_code in {401, 403}:
                        return LookupResult(reason="authentication")
                    if response.status_code == 429:
                        return LookupResult(
                            reason="rate_limited",
                            retry_after_seconds=retry_after_seconds(
                                response.headers.get("Retry-After"), now
                            ),
                        )
                    if response.status_code != 200:
                        return LookupResult(reason="source_error")
                    if (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        != "application/json"
                    ):
                        return LookupResult(reason="malformed")
                    # Decline compressed bodies, avoiding unbounded decompressor allocations.
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return LookupResult(reason="malformed")
                    length = response.headers.get("content-length")
                    if length is not None:
                        if not re.fullmatch(r"[0-9]+", length):
                            return LookupResult(reason="malformed")
                        if len(length) > 12 or int(length) > BODY_LIMIT:
                            return LookupResult(reason="body_too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                        if len(body) + len(chunk) > BODY_LIMIT:
                            return LookupResult(reason="body_too_large")
                        body.extend(chunk)
                    result = normalize_response(bytes(body))
                    if result.reason == "rate_limited":
                        return LookupResult(
                            reason="rate_limited",
                            retry_after_seconds=retry_after_seconds(
                                response.headers.get("Retry-After"), self._clock()
                            ),
                        )
                    return result
        except (TimeoutError, httpx.TimeoutException):
            return LookupResult(reason="timeout")
        except Exception:
            # Includes malformed stream/transport failures; never retain exception text.
            # CancelledError derives from BaseException and still propagates to the owner.
            return LookupResult(reason="source_error")
        finally:
            self._client.cookies.clear()
            _private_request.reset(token)
