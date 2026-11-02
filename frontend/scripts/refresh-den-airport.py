"""Refresh the bounded KDEN reference overlay from official FAA public services.

Run manually, never on startup. Uses only Python's standard library.
Review the manifest/effective cycle and data diff before committing an update.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OUTPUT = Path(__file__).resolve().parents[1] / "public" / "airports"
BASE = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
LAYERS = {
    "runway": ("AM_Runway", "bc80e5ca97804c2fbc0c10482377adf5"),
    "taxiway": ("AM_Taxiway", "d1c02f9f3f7144af8ead3aca44961c59"),
    "building": ("AM_Building", "7bca2c0cf31f43b89601433eda009312"),
    "apron": ("AM_Apron", "e74412cf6a2345eb9974fa21b6faa225"),
}


def get(url):
    request = Request(url, headers={"User-Agent": "SkyTrace-airport-reference/0.2"})
    with urlopen(request, timeout=30) as response:
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise ValueError("Airport response exceeds size limit")
    value = json.loads(body)
    if "error" in value:
        raise ValueError(value["error"])
    return value


def coordinates(value):
    if isinstance(value[0], (float, int)):
        lon, lat = value[:2]
        if not (-104.76 < lon < -104.6 and 39.79 < lat < 39.94):
            raise ValueError("Geometry outside the DEN airport envelope")
        return [round(lon, 7), round(lat, 7)]
    return [coordinates(child) for child in value]


def main():
    features, sources = [], []
    for kind, (name, item_id) in LAYERS.items():
        item_url = f"https://www.arcgis.com/sharing/rest/content/items/{item_id}?f=json"
        metadata = get(item_url)
        service = f"{BASE}/{name}/FeatureServer"
        if metadata["url"] != service or metadata["access"] != "public":
            raise ValueError("FAA service identity/access changed")
        query = {
            "where": "ICAO_ID = 'KDEN'",
            "outFields": "*",
            "outSR": 4326,
            "returnGeometry": "true",
            "orderByFields": "OBJECTID",
            "resultRecordCount": 2000,
            "f": "geojson",
        }
        url = f"{service}/0/query?{urlencode(query)}"
        result = get(url)
        if result.get("exceededTransferLimit") or len(result["features"]) >= 2000:
            raise ValueError("Airport query truncated; review the fetch limit")
        for feature in result["features"]:
            props = feature["properties"]
            if props["ICAO_ID"] != "KDEN":
                raise ValueError("Unexpected airport")
            geometry = feature["geometry"]
            if geometry["type"] not in ("Polygon", "MultiPolygon"):
                raise ValueError("Unexpected airport geometry")
            features.append(
                {
                    "type": "Feature",
                    "id": f"den-{kind}-{props['OBJECTID']}",
                    "properties": {
                        "kind": kind,
                        "name": props.get("DESIGNATOR") or props.get("RWY_ID") or "",
                        "airport": "KDEN",
                        "source_objectid": props["OBJECTID"],
                        "operational_status": props.get(
                            "RWY_OPER", props.get("TWY_OPER", props.get("APRON_OPER"))
                        ),
                    },
                    "geometry": {
                        "type": geometry["type"],
                        "coordinates": coordinates(geometry["coordinates"]),
                    },
                }
            )
        sources.append(
            {
                "kind": kind,
                "count": len(result["features"]),
                "item_url": item_url,
                "query_url": url,
                "description": re.sub("<[^>]+>", "", metadata["description"]).strip(),
                "license": re.sub("<[^>]+>", "", metadata["licenseInfo"]).strip(),
                "credit": metadata["accessInformation"],
            }
        )
    if not features:
        raise ValueError("No airport features returned")
    body = (
        json.dumps(
            {"type": "FeatureCollection", "features": features}, separators=(",", ":")
        )
        + "\n"
    )
    manifest = {
        "airport": "KDEN",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "feature_count": len(features),
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "coordinate_reference": "WGS84 longitude, latitude; no measured height",
        "modifications": "KDEN filter; selected attributes; coordinates rounded to 7 decimal places; no topology simplification",
        "sources": sources,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "den.geojson").write_text(body)
    (OUTPUT / "den-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Saved {len(features)} FAA reference features ({len(body):,} bytes)")
    print(
        "Review/update source dates and counts in public/map-attribution.html before release."
    )


if __name__ == "__main__":
    main()
