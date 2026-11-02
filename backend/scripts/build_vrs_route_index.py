#!/usr/bin/env python3
"""Build a global VRS route and airport index from one pinned source checkout.

This is intentionally offline.  It verifies every route and airport CSV against
a compact bundled Git-tree manifest (or an explicit freshly downloaded tree),
performs no telemetry or route-endpoint requests, and emits
only the fields defined by VRS schema-01 that the application consumes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parents[2]
COMMIT = "ba4454ca8b96c0302a0e037a644d7504d9b5beb0"
DEFAULT_OUTPUT = ROOT / "backend/src/skytrace/assets/vrs-standing-routes.sqlite3"
DEFAULT_MANIFEST = ROOT / "backend/src/skytrace/assets/vrs-standing-data-manifest.json"


def git_blob_sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_files(tree: dict, prefix: str) -> dict[str, str]:
    return {
        entry["path"]: entry["sha"]
        for entry in tree["tree"]
        if entry.get("type") == "blob"
        and entry["path"].startswith(prefix)
        and entry["path"].endswith(".csv")
    }


def verification_manifest(tree: dict) -> dict[str, object]:
    if tree.get("sha") != "09a7d864d71ff1cc6134aff723bd08dd9675c280":
        raise ValueError("Unexpected VRS source tree")
    files = source_files(tree, "routes/schema-01/") | source_files(tree, "airports/schema-01/")
    return {
        "schema_version": 1,
        "source": {
            "commit": COMMIT,
            "tree": tree["sha"],
            "archive_url": f"https://codeload.github.com/vradarserver/standing-data/tar.gz/{COMMIT}",
            "tree_url": f"https://api.github.com/repos/vradarserver/standing-data/git/trees/{COMMIT}?recursive=1",
        },
        "files": dict(sorted(files.items())),
    }


def load_manifest(path: Path) -> tuple[dict[str, str], dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    source = value.get("source")
    files = value.get("files")
    if (
        value.get("schema_version") != 1
        or not isinstance(source, dict)
        or source.get("commit") != COMMIT
        or source.get("tree") != "09a7d864d71ff1cc6134aff723bd08dd9675c280"
        or not isinstance(files, dict)
        or not all(isinstance(path, str) and isinstance(sha, str) for path, sha in files.items())
    ):
        raise ValueError("Invalid VRS source verification manifest")
    return files, source


def write_database(payload: dict[str, object], output: Path) -> None:
    """Write a compact, read-only-at-runtime index without runtime dict expansion."""

    source = payload["source"]
    routes = payload["routes"]
    airports = payload["airports"]
    assert isinstance(source, dict) and isinstance(routes, dict) and isinstance(airports, dict)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    connection = sqlite3.connect(output)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE routes (
                callsign TEXT PRIMARY KEY, airport_codes TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE airports (
                code TEXT PRIMARY KEY, name TEXT NOT NULL, icao TEXT, iata TEXT,
                latitude REAL, longitude REAL
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            ((key, json.dumps(value, separators=(",", ":"))) for key, value in source.items()),
        )
        connection.executemany(
            "INSERT INTO routes VALUES (?, ?)",
            ((key, json.dumps(value, separators=(",", ":"))) for key, value in routes.items()),
        )
        connection.executemany(
            "INSERT INTO airports VALUES (?, ?, ?, ?, ?, ?)",
            ((key, *value) for key, value in airports.items()),
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def check_database(payload: dict[str, object], output: Path) -> bool:
    if not output.is_file():
        return False
    source = payload["source"]
    routes = payload["routes"]
    airports = payload["airports"]
    assert isinstance(source, dict) and isinstance(routes, dict) and isinstance(airports, dict)
    connection = sqlite3.connect(f"file:{output}?mode=ro&immutable=1", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        return (
            int(connection.execute("SELECT count(*) FROM routes").fetchone()[0]) == len(routes)
            and int(connection.execute("SELECT count(*) FROM airports").fetchone()[0])
            == len(airports)
            and all(
                metadata.get(key) == json.dumps(value, separators=(",", ":"))
                for key, value in source.items()
            )
            and connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def build(
    source_root: Path, retrieved_at: str, files: dict[str, str], source: dict[str, object]
) -> dict[str, object]:
    """Read and verify the pinned source before creating the SQLite payload."""
    route_files = {path: sha for path, sha in files.items() if path.startswith("routes/schema-01/")}
    airport_files = {
        path: sha for path, sha in files.items() if path.startswith("airports/schema-01/")
    }
    if not route_files or not airport_files:
        raise ValueError("VRS verification manifest has no route or airport CSVs")
    routes: dict[str, list[str]] = {}
    for relative_path, expected_sha in sorted(route_files.items()):
        path = source_root / relative_path
        if git_blob_sha1(path) != expected_sha:
            raise ValueError(f"Pinned VRS blob changed: {relative_path}")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                callsign = row.get("Callsign", "").strip().upper()
                codes = [
                    code for code in row.get("AirportCodes", "").strip().upper().split("-") if code
                ]
                if not callsign or not 2 <= len(codes) <= 12 or callsign in routes:
                    raise ValueError(f"Malformed VRS route row: {callsign!r}")
                routes[callsign] = codes
    airports: dict[str, list[object | None]] = {}
    for relative_path, expected_sha in sorted(airport_files.items()):
        path = source_root / relative_path
        if git_blob_sha1(path) != expected_sha:
            raise ValueError(f"Pinned VRS blob changed: {relative_path}")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                code = row.get("Code", "").strip().upper()
                name = row.get("Name", "").strip()
                if not code or not name or code in airports:
                    raise ValueError(f"Malformed VRS airport row: {code!r}")
                latitude, longitude = (
                    row.get("Latitude", "").strip(),
                    row.get("Longitude", "").strip(),
                )
                airports[code] = [
                    name,
                    row.get("ICAO", "").strip().upper() or None,
                    row.get("IATA", "").strip().upper() or None,
                    float(latitude) if latitude else None,
                    float(longitude) if longitude else None,
                ]
    return {
        "schema_version": 1,
        "source": {
            "name": "VRS standing data",
            "license": "CC0-1.0",
            "url": f"https://github.com/vradarserver/standing-data/tree/{COMMIT}",
            "commit": COMMIT,
            "tree": source["tree"],
            "retrieved_at": retrieved_at,
            "route_files": len(route_files),
            "airport_files": len(airport_files),
        },
        "routes": dict(sorted(routes.items())),
        "airports": dict(sorted(airports.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--retrieved-at", required=True, help="UTC ISO-8601 retrieval timestamp")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--tree",
        type=Path,
        help="Fresh GitHub recursive-tree JSON; replaces the bundled manifest for this build",
    )
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="Write the compact source verification manifest derived from --tree",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write_manifest and args.tree is None:
        parser.error("--write-manifest requires --tree")
    if args.tree is not None:
        tree = json.loads(args.tree.read_text(encoding="utf-8"))
        manifest = verification_manifest(tree)
        if args.write_manifest:
            args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.write_manifest.write_text(
                json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
        files = manifest["files"]
        source = manifest["source"]
    else:
        files, source = load_manifest(args.manifest)
    payload = build(args.source_root, args.retrieved_at, files, source)
    if args.check:
        if not check_database(payload, args.output):
            raise SystemExit("VRS route index is not reproducible; run build_vrs_route_index.py")
        return
    write_database(payload, args.output)


if __name__ == "__main__":
    main()
