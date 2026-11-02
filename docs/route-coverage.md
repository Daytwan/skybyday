# SkybyDay route coverage

Update 24 October 2026: normal operation now resolves all tracked callsigns
locally from the full pinned VRS SQLite index. The selected-aircraft mirror and
its backoff below remain only as a missing-index fallback. See
[route snapshot](route-reference-snapshot.md) and [world experience verification](world-experience-verification-2026-10-24.md)
for the current implementation and new six-region denominators. The earlier
source study below is preserved as historical evidence.

This document records the route-enrichment decision for the R4 local release.
Departure and destination fields are always present in the aircraft inspector, but
they remain `Unknown` when no source-supported reference exists. A standing route
is never presented as a confirmed current flight.

## Current source and lookup policy

SkybyDay uses the ADSB.lol VRS standing-data mirror for selected, currently cached
live aircraft. The upstream [route schema](https://raw.githubusercontent.com/vradarserver/standing-data/main/routes/schema-01/README.md)
describes one row per normalized callsign and an ordered airport-code chain. It
does not provide a flight date, dispatch record, aircraft-to-flight identity, or
current-leg confirmation. The upstream VRS repository dedicates its standing data
under CC0; ADSB.lol's telemetry API data is ODbL. The mirror attribution does not
establish additional rights over unrelated ADSB.lol datasets.

The backend now applies the VRS normalization rules before lookup: uppercase and
trim the identifier, remove leading flight-number zeroes, and enforce the VRS
number shape. Two-character or mixed airline codes are rejected unless an exact,
verified IATA-to-ICAO alias is supplied to `RouteService`; no airline prefix is
guessed. Registrations and callsigns without a published standing route remain
unmatched. The normalized lookup key is returned as `lookup_callsign`, while the
source callsign remains available as `callsign`.

The API response includes a `reason` alongside `status`: `available`,
`multi_leg_reference`, `unmatched`, `invalid_callsign`, `cooldown`,
`rate_limited`, `source_error`, or `malformed`. `current_flight_confirmed` stays
false for every response. Multi-airport references retain their complete ordered
chain and do not choose a departure/destination leg.

Lookups remain serialized behind one process-wide lock with a ten-second minimum
request gap, bounded response/deadline limits, negative caching, and
`Retry-After` handling. Throttling and failures use a shared five-minute minimum
exponential backoff with positive jitter, bounded at one hour unless the source
requires a longer `Retry-After`. Cached failures report the remaining cooldown.
The browser schedules at most one cancellable automatic
retry for the currently selected aircraft after a cooldown response. Changing the
selection or leaving replay cancels the request and timer. This fixes a preventable
manual-retry gap without multiplying upstream requests. The full advertised delay
is respected; a delay exceeding the browser timer range requires manual retry.

## Official endpoint probe and source decision

On 21 October 2026 at 19:39:24 UTC, one bounded probe was made to the documented
ADSB.lol route endpoint:

```text
GET https://api.adsb.lol/api/0/route/UAL123/39.8561/-104.6737
User-Agent: SkybyDay/0.1 (self-hosted aviation explorer)
```

The response was HTTP 500 with `Internal Server Error`, no route payload, and
headers advertising a dynamic limit of `4` requests per one-second window, `4`
remaining, and a daily window of `500`. No retry or second probe was made. The
official implementation documents `/api/0/route/{callsign}/{lat}/{lng}` as a
route-plus-`plausible` endpoint and `/api/0/routeset` as a POST of at most 100
`{callsign,lat,lng}` records, but this release does not adopt that endpoint based
on a failed response. Its `plausible` geometry signal would not prove the current
flight or identify a current leg in any case.

The source implementation and schema are documented at [ADSB.lol's route
implementation](https://raw.githubusercontent.com/adsblol/api/main/src/adsb_api/utils/api_routes.py)
and [VRS standing data](https://github.com/vradarserver/standing-data). A later
integration requires a successful controlled probe, source-owner compatibility
review, and a measured coverage comparison before changing the adapter.

## Fixed-capture coverage baseline

This is an eligibility baseline, not a claim that every eligible callsign has a
route record. It uses the two saved ADSB.lol captures already in the repository;
no new telemetry or bulk route requests were made.

| Capture | Aircraft | Callsigns | Previous adapter pattern | VRS-normalized candidates | Registration/other |
| --- | ---: | ---: | ---: | ---: | ---: |
| `2026-10-18-den-phase2` (18 Oct capture) | 55 | 55 | 44 | 44 | 11 |
| `den-check` (20 Oct capture) | 29 | 28 | 23 | 23 | 5 |
| Combined records (not deduplicated) | 84 | 83 | 67 | 67 | 16 |

The normalization change therefore does not claim increased route coverage on
these captures. It makes the accepted key semantics match VRS and provides a safe
place for future verified aliases. Actual source coverage must be measured as
`reference / candidate`, with the capture timestamp, callsign, source response,
and whether the result is a two-airport or multi-leg reference retained in the
report. Private, registration, missing, reused, ambiguous, and stale callsigns
must be reported separately.

## Remaining blocker

No account-free public source reviewed supplies dispatch-grade current departure
and destination for every live aircraft. OpenSky's official flights endpoints
are derived by a nightly batch and do not provide today's live routes; see its
[FAQ](https://opensky-network.org/about/faq) and [REST documentation](https://openskynetwork.github.io/opensky-api/rest.html).
ADSBdb exposes route-shaped data, but its own repository states that the route
data may not be copied, published, or incorporated into another database without
permission; it is therefore not integrated. The all-aircraft route requirement
remains open pending a source with suitable live coverage and redistribution terms.
