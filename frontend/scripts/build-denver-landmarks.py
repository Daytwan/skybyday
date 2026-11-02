"""Convert one manual Overpass export to an ODbL Denver reference layer.

Query (https://overpass-api.de/api/interpreter; no runtime queries):
[out:json][timeout:25][maxsize:33554432];(
way["building"]["height"](39.728,-105.013,39.766,-104.967);
node["natural"="peak"]["name"](39.50,-105.75,40.05,-105.15););out geom;
Usage: python frontend/scripts/build-denver-landmarks.py export.json
Only explicit height tags are used. Storey counts are never converted to heights.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

raw = json.loads(Path(sys.argv[1]).read_text())
features = []
for entry in raw["elements"]:
    tags = entry.get("tags", {})
    if entry["type"] == "way":
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)\s*(m|ft|')?", tags.get("height", "")
        )
        if not match:
            continue
        height = float(match[1]) * (0.3048 if match[2] in ("ft", "'") else 1)
        coordinates = [[p["lon"], p["lat"]] for p in entry.get("geometry", [])]
        if (
            not 1 <= height <= 400
            or len(coordinates) < 4
            or coordinates[0] != coordinates[-1]
        ):
            continue
        geometry = {"type": "Polygon", "coordinates": [coordinates]}
        properties = {
            "kind": "building",
            "name": tags.get("name", ""),
            "height_m": height,
        }
    else:
        geometry = {"type": "Point", "coordinates": [entry["lon"], entry["lat"]]}
        properties = {"kind": "peak", "name": tags["name"]}
    features.append(
        {
            "type": "Feature",
            "id": f"{entry['type']}/{entry['id']}",
            "properties": properties,
            "geometry": geometry,
        }
    )
data = (
    json.dumps(
        {"type": "FeatureCollection", "features": features},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    + "\n"
).encode()
target = Path(__file__).resolve().parents[1] / "public/landmarks"
target.mkdir(parents=True, exist_ok=True)
(target / "denver.geojson").write_bytes(data)
manifest = {
    "source": "https://www.openstreetmap.org/copyright",
    "license": "ODbL 1.0",
    "snapshot_at": raw["osm3s"]["timestamp_osm_base"],
    "sha256": hashlib.sha256(data).hexdigest(),
    "buildings": sum(f["properties"]["kind"] == "building" for f in features),
    "peaks": sum(f["properties"]["kind"] == "peak" for f in features),
    "limitations": "Partial downtown footprints with OSM-reported heights; simplified flat-roof volumes, not surveyed or photorealistic models. Peaks are named reference points, not elevation measurements.",
}
(target / "denver-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(manifest)
