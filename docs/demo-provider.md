# Public demo aircraft provider

The hosted personal showcase uses **ADSB.fi**, explicitly selected with
`SKYTRACE_PROVIDER=adsbfi`. It requests the documented public regional endpoint
`https://opendata.adsb.fi/api/v3/lat/39.8561/lon/-104.6737/dist/60` through one
shared poller, at most once per 30 seconds. Visitor count does not multiply
upstream requests. Errors retain backoff or suspend the feed; there is no
automatic provider or synthetic fallback.

[ADSB.fi's documentation and terms](https://github.com/adsbfi/opendata/blob/main/README.md)
describe personal, non-commercial use, require a credit and homepage link, and
limit the public API to one request per second. The app displays ADSB.fi attribution
and a link to those terms. The data is **not covered by this repository's MIT
license or ADSB.lol's ODbL license**. Raw ADSB.fi captures are not shipped in the
repository or release archives. Commercial or higher-rate use needs a separate
arrangement with the provider.

The v3 regional endpoint documents the ADSBexchange v2 response format. The
adapter reuses the bounded wire decoder with distinct source identity and terms,
retaining timestamp checks, position age, geometric/barometric separation, and
response size limits. Unknown values stay unknown. Tests use artificial wire
data. Route references come only from the bundled CC0 VRS index, and are never
confirmed current-flight plans.

This demo keeps recording, AirLabs association, and the multiple-hub preset
disabled for ADSB.fi. The optional history schema has not been migrated for this
provider. ADSB.lol remains available as an explicitly selected adapter; it
returned `403 banned` from this hosting server on 2 November 2026. A single
ADSB.fi regional request from the same server returned HTTP 200 and four aircraft
at approximately 11:19 UTC. That probe establishes regional access at that time,
not universal coverage or guaranteed availability.
