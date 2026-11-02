from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skytrace.demo import create_app


def test_showcase_serves_only_build_and_preserves_api(tmp_path: Path, monkeypatch) -> None:
    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<h1>SkybyDay demo</h1>")
    (build / "bundle.js").write_text("/* bundled app */" * 200)
    (tmp_path / ".env").write_text("private")
    monkeypatch.setenv("SKYBYDAY_STATIC_DIR", str(build))
    monkeypatch.setenv("SKYTRACE_PROVIDER", "synthetic")
    monkeypatch.chdir(build)
    with TestClient(create_app()) as client:
        assert client.get("/").text == "<h1>SkybyDay demo</h1>"
        assert client.get("/bundle.js").headers["content-encoding"] == "gzip"
        assert client.get("/api/health").json()["telemetry"] == "synthetic"
        assert client.get("/api/history/status").json()["enabled"] is False
        assert client.get("/.env").status_code == 404
        assert client.get("/%2e%2e/.env").status_code == 404
        with client.websocket_connect(
            "/api/ws", headers={"Origin": "http://127.0.0.1:8000"}
        ) as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_showcase_requires_a_build(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKYBYDAY_STATIC_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="Build the frontend first"):
        create_app()
