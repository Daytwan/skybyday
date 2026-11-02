"""Build bounded City and County of Denver building-volume snapshots.

This is a manual release tool, never a browser/runtime data source. It downloads
two fixed envelopes from the official 2022 Building Outlines feature service,
checks the service identity and units, converts the published US survey feet
attributes to metres, and writes small view-driven JSON chunks.

Usage: python frontend/scripts/build-denver-buildings.py
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVICE = (
    "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services/"
    "ODC_PROP_BUILDINGOUTLINES_A/FeatureServer/111"
)
ITEM_ID = "04e5069b4cf843d2bfe15ad0d1c66e00"
OUT_FIELDS = "OBJECTID,BUILDING_I,BLDG_HEIGH,GROUND_ELE,BLDG_TYPE,SOURCE"
WHERE = "BLDG_HEIGH IS NOT NULL AND BLDG_HEIGH > 0 AND GROUND_ELE IS NOT NULL"
PAGE_SIZE = 2000
CHUNK_DEGREES = 0.01
US_SURVEY_FOOT_METRES = 1200 / 3937
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
REGIONS = {
    "downtown": (-105.013, 39.728, -104.967, 39.766),
    "den": (-104.76, 39.79, -104.60, 39.94),
}
TARGET = Path(__file__).resolve().parents[1] / "public/landmarks/denver-buildings"


def request_json(url: str, *, attempts: int = 3) -> tuple[dict[str, Any], bytes]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SkybyDay municipal snapshot builder/0.1"},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                size = int(response.headers.get("Content-Length", 0))
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"response too large: {size} bytes")
                body = response.read(MAX_DOWNLOAD_BYTES + 1)
                if len(body) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("response exceeded 40 MiB bound")
                parsed = json.loads(body)
                if "error" in parsed:
                    raise ValueError(f"ArcGIS error: {parsed['error']}")
                return parsed, body
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt + 1 < attempts:
                delay = min(30, int(exc.headers.get("Retry-After", "5")))
                time.sleep(max(1, delay))
                continue
            raise
    raise RuntimeError("request attempts exhausted")


def query_url(bounds: tuple[float, float, float, float], offset: int) -> str:
    params = {
        "where": WHERE,
        "geometry": ",".join(str(value) for value in bounds),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": OUT_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "OBJECTID ASC",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "f": "geojson",
    }
    return f"{SERVICE}/query?{urllib.parse.urlencode(params)}"


def validate_service() -> dict[str, Any]:
    metadata, _ = request_json(f"{SERVICE}?f=pjson")
    if metadata.get("serviceItemId") != ITEM_ID or metadata.get("id") != 111:
        raise ValueError("official building service identity changed")
    spatial_reference = metadata.get("extent", {}).get("spatialReference", {})
    if spatial_reference.get("latestWkid", spatial_reference.get("wkid")) != 2877:
        raise ValueError("expected Colorado Central State Plane EPSG:2877")
    if metadata.get("geometryProperties", {}).get("units") != "esriFeet":
        raise ValueError("service geometry units are no longer feet")
    fields = {field["name"] for field in metadata.get("fields", [])}
    if not set(OUT_FIELDS.split(",")).issubset(fields):
        raise ValueError("required building attributes changed")
    if metadata.get("maxRecordCount") != PAGE_SIZE:
        raise ValueError("review changed ArcGIS page limit before refreshing")
    return metadata


def polygons(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon":
        candidates = [coordinates]
    elif kind == "MultiPolygon":
        candidates = coordinates
    else:
        raise ValueError(f"unexpected geometry type: {kind}")
    result = []
    for polygon in candidates:
        clean_polygon = []
        for ring in polygon:
            clean_ring = [
                [round(float(lon), 7), round(float(lat), 7)] for lon, lat in ring
            ]
            if len(clean_ring) < 4 or clean_ring[0] != clean_ring[-1]:
                raise ValueError("invalid or open building ring")
            if any(
                not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in clean_ring
            ):
                raise ValueError("building coordinate outside WGS84 bounds")
            clean_polygon.append(clean_ring)
        if clean_polygon:
            result.append(clean_polygon)
    return result


def centroid(polygons_value: list[list[list[list[float]]]]) -> tuple[float, float]:
    points = polygons_value[0][0][:-1]
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def chunk_key(lon: float, lat: float) -> str:
    x = math.floor((lon + 180) / CHUNK_DEGREES)
    y = math.floor((lat + 90) / CHUNK_DEGREES)
    return f"{y}-{x}"


def main() -> None:
    metadata = validate_service()
    seen: dict[int, list[Any]] = {}
    page_hashes: list[str] = []
    downloaded = 0
    source_types: set[str] = set()
    for region, bounds in REGIONS.items():
        offset = 0
        while True:
            response, body = request_json(query_url(bounds, offset))
            downloaded += len(body)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise ValueError("bounded snapshot exceeded 40 MiB")
            page_hashes.append(hashlib.sha256(body).hexdigest())
            features = response.get("features", [])
            for feature in features:
                properties = feature.get("properties", {})
                object_id = int(properties["OBJECTID"])
                height_feet = float(properties["BLDG_HEIGH"])
                ground_feet = float(properties["GROUND_ELE"])
                # Retain positive published values even when they fall below the
                # current five-foot capture rule; older records can predate it.
                if not 0 < height_feet <= 2000:
                    raise ValueError(f"implausible building height for {object_id}")
                if not 4000 <= ground_feet <= 7000:
                    raise ValueError(
                        f"implausible Denver ground elevation for {object_id}"
                    )
                geometry = polygons(feature["geometry"])
                lon, lat = centroid(geometry)
                if not any(
                    west <= lon <= east and south <= lat <= north
                    for west, south, east, north in REGIONS.values()
                ):
                    continue
                building_type = str(properties.get("BLDG_TYPE") or "Unknown")[:40]
                source_types.add(str(properties.get("SOURCE") or "Unknown")[:40])
                seen[object_id] = [
                    object_id,
                    round(height_feet * US_SURVEY_FOOT_METRES, 3),
                    round(ground_feet * US_SURVEY_FOOT_METRES, 3),
                    building_type,
                    geometry,
                ]
            if not response.get("properties", {}).get("exceededTransferLimit"):
                break
            if not features:
                raise ValueError(f"empty truncated page in {region}")
            offset += len(features)

    chunks: dict[str, list[list[Any]]] = defaultdict(list)
    for building in seen.values():
        lon, lat = centroid(building[4])
        chunks[chunk_key(lon, lat)].append(building)

    TARGET.mkdir(parents=True, exist_ok=True)
    manifest_chunks = {}
    for key, buildings in sorted(chunks.items()):
        buildings.sort(key=lambda row: row[0])
        payload = (
            json.dumps(
                {"version": 1, "buildings": buildings},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        filename = f"{key}.json"
        (TARGET / filename).write_bytes(payload)
        all_points = [
            point
            for row in buildings
            for polygon in row[4]
            for ring in polygon
            for point in ring
        ]
        manifest_chunks[key] = {
            "count": len(buildings),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bounds": [
                min(point[0] for point in all_points),
                min(point[1] for point in all_points),
                max(point[0] for point in all_points),
                max(point[1] for point in all_points),
            ],
        }

    manifest = {
        "version": 1,
        "source": SERVICE,
        "source_item": f"https://www.arcgis.com/home/item.html?id={ITEM_ID}",
        "service_item_id": ITEM_ID,
        "title": "Building Outlines 2022",
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_last_edit_ms": metadata.get("editingInfo", {}).get("dataLastEditDate"),
        "coordinate_reference": "WGS84 output transformed by ArcGIS from EPSG:2877",
        "vertical_reference": "NAVD88(GEOID12A) orthometric heights",
        "source_units": "US survey feet",
        "conversion_metres_per_foot": US_SURVEY_FOOT_METRES,
        "height_semantics": "Highest roof point above the common lowest point",
        "building_count": len(seen),
        "chunk_degrees": CHUNK_DEGREES,
        "downloaded_bytes": downloaded,
        "source_attribute_values": sorted(source_types),
        "upstream_page_sha256": page_hashes,
        "regions": REGIONS,
        "chunks": manifest_chunks,
        "license": "City and County of Denver open-data as-is terms; not for engineering purposes",
        "limitations": (
            "2022 stereo-compiled roofprint polygons with one scalar height per roof level; "
            "not roof planes, facade textures, engineering data, or current construction status."
        ),
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode() + b"\n"
    (TARGET / "manifest.json").write_bytes(manifest_bytes)
    print(
        json.dumps(
            {
                "buildings": len(seen),
                "chunks": len(chunks),
                "downloaded_bytes": downloaded,
                "output_bytes": len(manifest_bytes)
                + sum(entry["bytes"] for entry in manifest_chunks.values()),
            }
        )
    )


if __name__ == "__main__":
    main()
