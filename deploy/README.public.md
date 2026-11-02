# SkybyDay

**A closer look at the skies.** An interactive 3D flight explorer built with
TypeScript, CesiumJS, Python, FastAPI, and OpenAI Codex.

[Open the demo](https://fly.donutmath.com) ·
[How to deploy](docs/deployment.md) · [Architecture](docs/architecture.md)

SkybyDay is a work-in-progress showcase by [Daytwan](https://github.com/Daytwan)
for a Codex Ambassador application. It brings live aircraft observations,
terrain, airport geometry, city labels, weather, and aircraft inspection into
one navigable globe.

## Try it

1. Open the demo and wait for the Denver aircraft feed.
2. Select an aircraft in the roster or on the globe. Inspect its reported
   position, altitude, callsign, and any available route reference.
3. Use **Map → Places to explore** to visit Denver International Airport,
   downtown Denver, and the Front Range. Drag and zoom to explore the globe.
4. Open **Weather** for the timestamped Denver observation.

The hosted demo tracks a 60-nautical-mile area around Denver, with one shared
ADSB.lol poll every 30 seconds regardless of viewer count. Worldwide map labels
do not imply worldwide traffic coverage. Route references are published
callsign associations, not confirmed current flight plans. Aircraft are never
silently replaced with fictional data if the live feed fails.

Historical recording/replay and the experimental AirLabs integration are
disabled on the public demo. Airport geometry is a dated reference snapshot
(cycle ending 29 October 2026); imagery and terrain have their own dates and
limits. This is a project demonstration, not an operational aviation tool.

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24, and a browser
with WebGL2. No API key or database is needed for the default live viewer.

```bash
git clone https://github.com/Daytwan/skybyday.git
cd skybyday
uv sync --project backend --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run --project backend --locked uvicorn skytrace.demo:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1 --ws websockets-sansio
```

Open <http://127.0.0.1:8000>. For frontend development, run
`npm --prefix frontend run dev` in another terminal and open
<http://127.0.0.1:5173>. Vite proxies the API to port 8000.

Copy `.env.example` to `.env` only if you need to override defaults. For an
explicitly fictional offline development feed, set `SKYTRACE_PROVIDER=synthetic`.
Choose **Map → Imagery → Offline world** to use bundled map imagery.

## Checks

```bash
uv run --project backend --locked pytest backend/tests
npm --prefix frontend run check
npm --prefix frontend run build
```

Tests use artificial fixtures. The optional PostGIS integration test requires a
separately configured test database. Browser interaction and hosted verification
are separate from unit tests.

## Project structure

| Directory | Purpose |
| --- | --- |
| `frontend/` | Cesium globe, aircraft display, explorer, and browser assets |
| `backend/` | Shared ingestion, HTTP/WebSocket API, weather, routes, and tests |
| `deploy/` | Single-service deployment and reverse-proxy configuration |
| `docs/` | Architecture, API, data sources, and deployment guide |

The original project name was SkyTrace. Python imports, CLI commands, and
`SKYTRACE_` environment variables retain that name for compatibility.

## Built with Codex

Codex assisted with implementation, debugging, tests, documentation, and preparing
this public demo. The repository shows the current application as a clean source
snapshot. Local credentials, raw telemetry captures, and private session notes
are excluded.

## License and attribution

Original code is [MIT licensed](LICENSE). Third-party data has separate terms:
ADSB.lol aircraft data uses [ODbL 1.0](https://www.adsb.lol/docs/privacy-license/);
the bundled VRS standing-route reference is CC0; GeoNames city labels are CC BY
4.0. CesiumJS is Apache-2.0. Map, terrain, airport, and building sources retain
their notices in [Map sources](frontend/public/map-attribution.html), also linked
from the running app. See [route provenance](docs/route-reference-snapshot.md)
and [map documentation](docs/maps.md).
