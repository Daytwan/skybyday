import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from skytrace.config import Settings
from skytrace.main import create_app


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Developer environment files and variables must not change test results."""
    for name in os.environ:
        if name.upper().startswith("SKYTRACE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(provider="synthetic"))
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(1):
            while app.state.runtime.ingestion.snapshot().status == "idle":
                await asyncio.sleep(0)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
