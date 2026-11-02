"""Serve the built showcase and existing API from one deployment process."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from skytrace.main import create_app as create_api


def create_app() -> FastAPI:
    default = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    directory = Path(os.environ.get("SKYBYDAY_STATIC_DIR", str(default))).resolve()
    if not (directory / "index.html").is_file():
        raise RuntimeError("Build the frontend first: npm --prefix frontend run build")
    app = create_api()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    # Register last so the existing HTTP and WebSocket API routes take priority.
    app.mount("/", StaticFiles(directory=directory, html=True), name="showcase")
    return app
