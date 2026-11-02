"""Build a bounded, offline gazetteer from a manually downloaded GeoNames cities500 ZIP.

Usage: python frontend/scripts/build-places.py /path/to/cities500.zip
Download: https://download.geonames.org/export/dump/cities500.zip (CC BY 4.0).
No service calls at runtime. Output is derived data, not covered by the code's MIT license.
"""

import hashlib
import json
import math
import sys
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

target = Path(__file__).resolve().parents[1] / "public/places"
target.mkdir(parents=True, exist_ok=True)
chunks = defaultdict(list)
major = []
with zipfile.ZipFile(sys.argv[1]) as archive:
    if archive.getinfo("cities500.txt").file_size > 150_000_000:
        raise ValueError("Unexpectedly large gazetteer")
    for line in archive.read("cities500.txt").decode("utf-8").splitlines():
        fields = line.split("\t")
        lat, lon, population = float(fields[4]), float(fields[5]), int(fields[14])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinate")
        row = [int(fields[0]), fields[1], lon, lat, population, fields[7] == "PPLC"]
        key = f"{min(17, math.floor((lat + 90) / 10))}-{min(35, math.floor((lon + 180) / 10))}"
        chunks[key].append(row)
        if population >= 500_000 or row[5]:
            major.append(row)


def write(name, value):
    data = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    (target / name).write_bytes(data)
    return {
        "count": len(value),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


manifest = {
    "source": "https://download.geonames.org/export/dump/cities500.zip",
    "license": "CC BY 4.0",
    "built_at": datetime.now(UTC).isoformat(),
    "source_sha256": hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest(),
    "columns": ["id", "name", "longitude", "latitude", "population", "capital"],
    "coverage": "GeoNames cities over 500 people or administrative seats down to PPLA4; incomplete gazetteer, not every settlement.",
    "chunks": {key: write(f"{key}.json", rows) for key, rows in sorted(chunks.items())},
    "major": write("major.json", major),
    "count": sum(map(len, chunks.values())),
}
(target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Built {manifest['count']} places in {len(chunks)} geographic chunks")
