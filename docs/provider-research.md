# Provider research and access constraints

Reviewed 2026-10-18 using official documentation and provider-maintained source.
No telemetry or historical dataset was downloaded in Phase 0–1. Phase 2 then
made one deliberate public point request after offline tests; its results are
recorded below. Phase 4 made one additional deliberate DEN request to verify the
globe; no historical dataset has been downloaded.

## ADSB.lol public API

The [public API page](https://www.adsb.lol/docs/open-data/api/) identifies open
access and ODbL 1.0. The [deployed OpenAPI document](https://api.adsb.lol/api/openapi.json)
and [interactive docs](https://api.adsb.lol/docs) describe:

- `GET /v2/point/{lat}/{lon}/{radius}`: decimal latitude first, longitude second;
  latitude −90…90, longitude −180…180; integer radius in nautical miles, up to 250.
- An envelope with `ac`, `now`, `ctime`, `ptime`, `msg`, and `total`. Aircraft
  fields include `hex`, `flight`, `lat`, `lon`, `alt_baro`, `alt_geom`, `gs`,
  `track`, `seen`, and `seen_pos`. Many observation fields are nullable/absent.
- The deployed description requests contacting the operator before production
  use and notes possible future feeder-issued API keys. Contact is a future
  deployment prerequisite, not something this project has done.

The [provider's API README](https://github.com/adsblol/api/blob/main/README.md)
states rate limits vary with load; it gives no fixed guaranteed request quota.
The reported previous 429 is consistent with needing conservative access. Use
one shared request per configured interval (initially 30 seconds), never per
browser. This is SkybyDay's policy, not a provider promise. Honor Retry-After
and backoff; stop repeated unauthorized/invalid requests. The exact scheduling
policy is in [architecture.md](architecture.md).

The [feeder re-api](https://www.adsb.lol/docs/feeders-only/re-api/) is a separate
IP-restricted service. The public point API does not grant access to it.

## Schema and timing

The [provider route implementation](https://github.com/adsblol/api/blob/main/src/adsb_api/utils/api_v2.py)
passes through readsb responses. Its OpenAPI declares an integer `now` but does
not specify a timestamp unit. The provider's
[ReAPI client](https://github.com/adsblol/api/blob/main/src/adsb_api/utils/reapi.py)
adds `jv2`. In upstream
[readsb's JSON v2 encoder](https://github.com/wiedehopf/readsb/blob/dev/api.c),
`jv2` selects millisecond epoch values for `now` and `ctime`; regular readsb JSON
instead divides that timestamp by 1000. Therefore the source-backed expected
public v2 unit is **milliseconds**, with `seen`/`seen_pos` still in seconds.
This was initially an inference from published implementation; Phase 2 also
confirmed it in the deployed response.

The single live DEN check received HTTP 200 at `2026-10-19T03:57:32.335989Z`
(October 18 in Denver). It returned integer `now=1792382252001`, corresponding
to `2026-10-19T03:57:32.001Z`, 56 records, and one non-ICAO address. The adapter
accepted 55 aircraft. Offline replay checked the epoch conversion, position
ages and numeric barometric-altitude conversions against that same capture.
Selected HTTP headers and ODbL attribution are saved alongside the ignored local
body. See [Phase 2 verification](phase-2-verification.md) for hashes and limits.
This demonstrates that one request, not ongoing availability or a fixed quota.

Phase 4's separate request received HTTP 200 at `2026-10-19T15:09:58.926562Z`.
All 130 records normalized and appeared on the local globe; 100 supplied
geometric altitude and 30 were marked as surface projections. The temporary
service used a 3,600-second interval and was stopped after browser verification,
before another request became due. Captured telemetry remains local with source
and license metadata. See [Phase 4 verification](phase-4-verification.md).

Synthetic fixtures pin the millisecond interpretation. The adapter never
guesses by timestamp magnitude or substitutes local receipt time for position
time. It checks snapshot age/future-clock tolerance and malformed data, and
omits positions whose age is missing. Position epoch seconds are
`now / 1000 - seen_pos`.

## Data licensing and history

Both the [API data](https://www.adsb.lol/docs/open-data/api/) and
[historical data](https://www.adsb.lol/docs/open-data/historical/) are documented
as ODbL 1.0. Historical files are published through daily GitHub releases in
[globe_history](https://github.com/adsblol/globe_history); availability is not a
guarantee of completeness, perpetual retention, or unlimited download capacity.
SkybyDay does not download those archives. Phase 6 records its own successful
live/synthetic provider observations into optional local PostGIS with bounded
retention and source/license metadata. Its history API queries only that local
database; a browser history query never increases upstream polling.

Data displays provide visible linked attribution, for example:
“Aircraft data: [ADSB.lol](https://www.adsb.lol/) ·
[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).” Keep source and
license metadata with exports and recorded fixtures. The
[ODbL text, sections 4.2–4.6](https://opendatacommons.org/licenses/odbl/1-0/)
covers notices, attribution, share-alike for publicly used derivative databases,
and provision of the derivative database or alterations in machine-readable
form. Review those conditions for the actual publication/export design before
distributing a derived historical database. Local storage and analysis are
within the license's grant subject to its conditions; historical data is not
relicensed as MIT code.

Phase 6 review (2026-10-19): the ADSB.lol documentation website returned 503,
so the official [API README](https://github.com/adsblol/api/blob/main/README.md)
and [history repository](https://github.com/adsblol/globe_history) were reviewed
alongside the ODbL text. The API README continues to describe dynamic rate
limits; no fixed numeric quota is promised. The history repository retains its
ODbL notice. No new live telemetry request or historical bulk download was made
for Phase 5/6 verification; repeatable checks used explicitly synthetic data.

The [privacy/license page](https://www.adsb.lol/privacy-license/) describes CC0
for data contributed by feeders. That contribution statement does not replace
the explicit ODbL license for the public API/history products.

## OpenSky: future adapter only

[OpenSky's official API docs](https://openskynetwork.github.io/opensky-api/rest.html)
describe state vectors, bounded geographic queries, credit quotas, and OAuth2
client credentials. Do not assume old Basic Authentication examples still work.
Its quota headers and retry behavior need a separate adapter. Review the current
[OpenSky terms linked from its API overview](https://openskynetwork.github.io/opensky-api/index.html)
before enabling access or combining datasets. No credentials or automatic
fallback are configured; silently merging providers would obscure source and
license boundaries.

## Worldwide-feed decision — October 2026

No documented, credential-free public endpoint was approved for a complete
worldwide live view. ADSB.lol documents point queries up to 250 NM and global
filtered subsets, but no unfiltered all-aircraft route; its public API is now
marked deprecated in favour of a feeder-restricted service. [ADSB.fi's open-data
README](https://github.com/adsbfi/opendata) documents its all-aircraft snapshot
as feeder-IP-only. Airplanes.live and ADSB One document identifier, filtered,
and radius queries rather than a public all-aircraft feed.

OpenSky documents global state vectors, but its current terms require prior
written agreement for operational REST use. This project has no agreement,
credentials, paid account, or feeder authorization, so it does not enable that
endpoint. `world_hubs_v1` is instead a finite ADSB.lol point-query sampling
profile: DEN (60 NM), LHR, HND, SYD, DXB, and GRU (120 NM each). A single shared
30-second slot rotates through six areas, so each is nominally refreshed every
180 seconds. It exposes per-area freshness/failure state, never claims global
completeness, and does not react to camera movement. The profile requires stale
threshold at least 420 seconds and expiry greater than stale; 420/900 are the
documented local settings.
