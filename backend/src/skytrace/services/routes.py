"""On-selection route references from ADSB.lol's VRS standing-data mirror.

These are callsign database matches, not live flight plans. Multi-leg entries
cannot establish the aircraft's current departure or destination.
"""

import asyncio
import json
import math
import random
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from skytrace.providers.adsblol import parse_retry_after
from skytrace.services.route_data import RouteReferenceIndex

VRS_CALLSIGN_PATTERN = re.compile(
    r"^(?P<code>[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])(?P<number>[0-9][A-Z0-9]*)$"
)
VRS_NUMBER_PATTERN = re.compile(r"^[0-9]{1,4}[A-Z]{0,2}$")


def normalize_vrs_callsign(
    callsign: str | None, *, icao_aliases: Mapping[str, str] | None = None
) -> str | None:
    """Return the exact VRS lookup key, or None when it cannot be verified.

    VRS accepts an ICAO/IATA airline code plus a flight number, strips leading
    zeroes from that number, and stores the airline's ICAO code.  We only map an
    IATA or mixed code when an explicitly supplied, verified alias says which
    ICAO code to use; there is no prefix guessing here.
    """

    if not callsign:
        return None
    match = VRS_CALLSIGN_PATTERN.fullmatch(callsign.strip().upper())
    if match is None:
        return None
    code = match.group("code")
    number = match.group("number").lstrip("0")
    if not number or number[0].isalpha():
        number = f"0{number}"
    if not VRS_NUMBER_PATTERN.fullmatch(number) or len(number) > 4:
        return None
    if not (len(code) == 3 and code.isalpha()):
        if icao_aliases is None:
            return None
        mapped = icao_aliases.get(code)
        if mapped is None:
            return None
        code = mapped.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            return None
    return f"{code}{number}"


class RouteAirport(BaseModel):
    icao: str = Field(pattern=r"^[A-Z0-9]{3,8}$")
    iata: str | None = Field(default=None, max_length=4)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class RouteResponse(BaseModel):
    callsign: str | None
    lookup_callsign: str | None = None
    status: Literal["reference", "unknown", "unavailable", "waiting"]
    airports: list[RouteAirport] = Field(default_factory=list, max_length=12)
    source: Literal["ADSB.lol / VRS standing data", "VRS standing-data snapshot"] = (
        "ADSB.lol / VRS standing data"
    )
    source_url: str = "https://github.com/vradarserver/standing-data"
    source_snapshot_commit: str | None = None
    source_snapshot_retrieved_at: datetime | None = None
    fetched_at: datetime | None = None
    retry_in_seconds: int = 0
    reason: (
        Literal[
            "available",
            "multi_leg_reference",
            "unmatched",
            "invalid_callsign",
            "cooldown",
            "rate_limited",
            "source_error",
            "malformed",
        ]
        | None
    ) = None
    current_flight_confirmed: Literal[False] = False


class RouteService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        icao_aliases: Mapping[str, str] | None = None,
        reference_index: RouteReferenceIndex | None = None,
    ) -> None:
        self.client = client
        self.icao_aliases = {
            key.upper(): value.upper() for key, value in (icao_aliases or {}).items()
        }
        self.reference_index = (
            RouteReferenceIndex.bundled() if reference_index is None else reference_index
        )
        self.lock = asyncio.Lock()
        self.cache: OrderedDict[str, tuple[float, RouteResponse]] = OrderedDict()
        self.next_fetch = 0.0
        self.failures = 0

    def close(self) -> None:
        """Release the immutable local index during application shutdown."""

        self.reference_index.close()

    def resolve_reference(self, callsign: str | None) -> RouteResponse:
        """Resolve one callsign from the pinned VRS snapshot without I/O.

        This returns standing-reference data only.  It cannot establish an
        aircraft's current leg, departure, destination, or dispatch record.
        """

        lookup_callsign = normalize_vrs_callsign(callsign, icao_aliases=self.icao_aliases)
        if lookup_callsign is None:
            return RouteResponse(callsign=callsign, status="unknown", reason="invalid_callsign")
        if not self.reference_index.source_commit:
            return RouteResponse(
                callsign=callsign,
                lookup_callsign=lookup_callsign,
                status="unavailable",
                reason="source_error",
            )
        airport_codes = self.reference_index.lookup(lookup_callsign)
        common = {
            "callsign": callsign,
            "lookup_callsign": lookup_callsign,
            "source": "VRS standing-data snapshot",
            "source_url": self.reference_index.source_url,
            "source_snapshot_commit": self.reference_index.source_commit or None,
            "source_snapshot_retrieved_at": self.reference_index.retrieved_at
            if self.reference_index.source_commit
            else None,
        }
        if airport_codes is None:
            return RouteResponse(status="unknown", reason="unmatched", **common)
        airports: list[RouteAirport] = []
        for airport_code in airport_codes:
            airport = self.reference_index.airport(airport_code)
            if airport is None:
                airports.append(RouteAirport(icao=airport_code))
                continue
            name, icao, iata, latitude, longitude = airport
            airports.append(
                RouteAirport(
                    icao=icao or airport_code,
                    iata=iata,
                    name=name,
                    latitude=latitude,
                    longitude=longitude,
                )
            )
        return RouteResponse(
            status="reference",
            airports=airports,
            reason="available" if len(airports) == 2 else "multi_leg_reference",
            **common,
        )

    async def get(self, callsign: str | None) -> RouteResponse:
        local = self.resolve_reference(callsign)
        # A complete pinned index avoids a network request for both found and
        # absent callsigns.  Keep the guarded mirror path only for an omitted
        # local asset, preserving its Retry-After and bounded-backoff behavior.
        if self.reference_index.source_commit:
            return local
        if local.reason == "invalid_callsign":
            return local
        lookup_callsign = local.lookup_callsign
        assert lookup_callsign is not None
        async with self.lock:
            cached = self.cache.get(lookup_callsign)
            if cached and time.monotonic() < cached[0]:
                self.cache.move_to_end(lookup_callsign)
                remaining = (
                    math.ceil(max(cached[0], self.next_fetch) - time.monotonic())
                    if cached[1].status == "unavailable"
                    else 0
                )
                return cached[1].model_copy(
                    update={"callsign": callsign, "retry_in_seconds": max(0, remaining)}
                )
            if time.monotonic() < self.next_fetch:
                return RouteResponse(
                    callsign=callsign,
                    lookup_callsign=lookup_callsign,
                    status="waiting",
                    retry_in_seconds=math.ceil(self.next_fetch - time.monotonic()),
                    reason="cooldown",
                )
            self.next_fetch = time.monotonic() + 10
            result = RouteResponse(
                callsign=callsign,
                lookup_callsign=lookup_callsign,
                status="unavailable",
                reason="source_error",
            )
            ttl = 300
            try:
                async with (
                    asyncio.timeout(10),
                    self.client.stream(
                        "GET",
                        f"https://vrs-standing-data.adsb.lol/routes/{lookup_callsign[:2]}/{lookup_callsign}.json",
                        timeout=8,
                        headers={"User-Agent": "SkybyDay/0.1 (self-hosted aviation explorer)"},
                    ) as response,
                ):
                    delay = parse_retry_after(
                        response.headers.get("retry-after"), datetime.now(UTC)
                    )
                    if delay is not None:
                        self.next_fetch = max(self.next_fetch, time.monotonic() + delay)
                        result.retry_in_seconds = math.ceil(delay)
                    if response.status_code == 404:
                        result.status = "unknown"
                        result.reason = "unmatched"
                        ttl = 3600
                    elif response.status_code == 429:
                        result.reason = "rate_limited"
                    else:
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 65_536:
                                raise ValueError("Route response too large")
                        value = json.loads(content)
                        if not isinstance(value, dict) or value.get("callsign") != lookup_callsign:
                            raise ValueError("Mismatched callsign")
                        airports = value.get("_airports")
                        if not isinstance(airports, list) or not 2 <= len(airports) <= 12:
                            result.status = "unknown"
                            result.reason = "unmatched"
                        else:
                            result.airports = [RouteAirport.model_validate(a) for a in airports]
                            result.status = "reference"
                            result.reason = (
                                "available" if len(airports) == 2 else "multi_leg_reference"
                            )
                        result.fetched_at = datetime.now(UTC)
                        ttl = 21_600
            except (httpx.HTTPError, TimeoutError):
                self.next_fetch = max(self.next_fetch, time.monotonic() + 300)
                result.reason = "source_error"
            except ValidationError:
                self.next_fetch = max(self.next_fetch, time.monotonic() + 300)
                result.reason = "malformed"
            except ValueError:
                self.next_fetch = max(self.next_fetch, time.monotonic() + 300)
                result.reason = "malformed"
            if result.status == "unavailable":
                self.failures = min(self.failures + 1, 5)
                # Positive jitter cannot shorten Retry-After or the five-minute floor.
                base = min(3600, 300 * 2 ** (self.failures - 1))
                backoff = min(3600, base + random.uniform(0, base * 0.2))
                self.next_fetch = max(self.next_fetch, time.monotonic() + backoff)
                ttl = max(ttl, self.next_fetch - time.monotonic())
            else:
                self.failures = 0
            result.retry_in_seconds = max(
                result.retry_in_seconds,
                math.ceil(max(0.0, self.next_fetch - time.monotonic()))
                if result.status == "unavailable"
                else 0,
            )
            self.cache[lookup_callsign] = (time.monotonic() + ttl, result)
            self.cache.move_to_end(lookup_callsign)
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
            return result
