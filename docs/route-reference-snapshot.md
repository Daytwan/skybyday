# Global VRS standing-route snapshot

SkybyDay bundles a local index from [VRS standing data](https://github.com/vradarserver/standing-data)
commit `ba4454ca8b96c0302a0e037a644d7504d9b5beb0`, released by that project under
[CC0-1.0](https://github.com/vradarserver/standing-data/blob/ba4454ca8b96c0302a0e037a644d7504d9b5beb0/LICENSE).
The asset contains 620,777 route records and 34,128 airport records. Source CSV
input was 19,037,919 bytes of route files plus 2,547,439 bytes of airport files;
the generated SQLite route/airport asset is about 19 MB and its verification
manifest is 167,844 bytes. The runtime opens the database immutable and uses a
bounded 256-airport cache; it does not decode the full route set into memory.

The VRS route schema stores a normalized callsign and an ordered airport-code
chain. The airport schema supplies each code's name, ICAO/IATA code, latitude,
and longitude. `RouteService.resolve_reference()` reads the bundled index only:
it makes no network request. It returns a standing reference, never proof of the
observed aircraft's current flight, departure, destination, current leg, or
dispatch plan. Multi-leg chains remain unassigned, and absent or invalid
callsigns remain explicit `unknown` results.

## Refresh procedure

This procedure makes no telemetry or ADSB.lol route-endpoint request. Download
the exact source archive and its recursive Git tree, then rebuild from the
extracted directory. The builder verifies every included CSV as a Git blob hash
against the tree before writing an asset.

```text
commit=ba4454ca8b96c0302a0e037a644d7504d9b5beb0
curl -L --fail -o /tmp/vrs-standing-data.tar.gz \
  https://codeload.github.com/vradarserver/standing-data/tar.gz/$commit
curl -L --fail -o /tmp/vrs-standing-data-tree.json \
  https://api.github.com/repos/vradarserver/standing-data/git/trees/$commit?recursive=1
mkdir -p /tmp/vrs-standing-data-source
tar -xzf /tmp/vrs-standing-data.tar.gz -C /tmp/vrs-standing-data-source
python3 backend/scripts/build_vrs_route_index.py \
  --source-root /tmp/vrs-standing-data-source/standing-data-$commit \
  --tree /tmp/vrs-standing-data-tree.json \
  --write-manifest backend/src/skytrace/assets/vrs-standing-data-manifest.json \
  --retrieved-at 2026-10-23T09:09:54Z
```

For a reproducibility check using the committed compact manifest, repeat the
builder with the same extracted source, retrieval timestamp, and `--check`.
Changing the pinned commit requires updating the commit constant and recording a
new dated coverage measurement. A source snapshot alone does not establish
current-flight coverage.
