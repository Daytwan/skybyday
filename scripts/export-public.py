#!/usr/bin/env python3
"""Create a fresh public source snapshot without local history or private files."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [".gitignore", ".env.example", ".nvmrc", "LICENSE", "compose.yaml"]
DIRECTORIES = ["backend", "frontend", "deploy", "scripts"]
DOCS = [
    "api.md", "architecture.md", "data-model.md", "decisions.md", "deployment.md",
    "history.md", "maps.md", "provider-research.md", "roadmap.md",
    "route-reference-snapshot.md", "route-coverage.md", "route-source-coverage.md",
    "skyline-sources.md", "terrain-sources.md",
]
EXCLUDED = {
    ".git", ".venv", "node_modules", "dist", "__pycache__", ".pytest_cache",
    ".ruff_cache", "htmlcov", "coverage", ".agents", ".codex", "data",
    "artifacts", "output", ".mypy_cache", ".playwright-cli",
}
SECRETS = {
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "AWS access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "OpenAI key": re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}"),
}


def allowed(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
        return False
    if relative.is_relative_to("frontend/public/cesium"):
        return False  # Reproduced from the pinned Cesium package at build time.
    if path.name.startswith(".env") or path.name.endswith((".pyc", ".log", ".pem")):
        return False
    if path.name.endswith(".sqlite3"):
        return relative.as_posix() == "backend/src/skytrace/assets/vrs-standing-routes.sqlite3"
    return not path.name.endswith((".db", ".sqlite", ".tsbuildinfo"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New, nonexistent output directory")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must not exist; create a fresh snapshot instead of overwriting work")

    selected = {ROOT / name for name in FILES}
    selected.update(ROOT / "docs" / name for name in DOCS)
    for folder in DIRECTORIES:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and allowed(path):
                selected.add(path)
    manifest = []
    for path in sorted(selected):
        content = path.read_bytes()
        for name, pattern in SECRETS.items():
            if pattern.search(content):
                raise SystemExit(f"Possible {name} in {path.relative_to(ROOT)}; review before export")
        manifest.append({"path": path.relative_to(ROOT).as_posix(),
                         "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})

    output.mkdir(parents=True)
    for path in sorted(selected):
        target = output / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.copy2(ROOT / "deploy/README.public.md", output / "README.md")
    (output.parent / (output.name + "-manifest.json")).write_text(json.dumps(manifest, indent=2))
    print(f"Exported {len(selected) + 1} public files to {output}")
    print("Known credential patterns: no matches. Local Git history and private files excluded.")


if __name__ == "__main__":
    main()
