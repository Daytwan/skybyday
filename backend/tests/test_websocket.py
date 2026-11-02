import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skytrace.api import aircraft_stream
from skytrace.config import Settings
from skytrace.main import create_app
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.ingestion import IngestionService
from skytrace.services.runtime import TelemetryRuntime

ORIGIN = "http://127.0.0.1:5173"


def test_websocket_initial_snapshot_reconnect_and_rest_share_state() -> None:
    with TestClient(create_app(Settings(provider="synthetic"))) as client:
        with client.websocket_connect("/api/ws", headers={"origin": ORIGIN}) as socket:
            frame = socket.receive_json()
            if frame["data"]["status"] == "idle":
                frame = socket.receive_json()
            cached = client.get("/api/aircraft").json()
            assert frame["type"] == "snapshot"
            assert frame["schema_version"] == 1
            assert len(frame["data"]["aircraft"]) == 2
            assert frame["stream_id"] == cached["stream_id"]
            assert frame["data"]["aircraft"] == cached["data"]["aircraft"]
        with client.websocket_connect("/api/ws", headers={"origin": ORIGIN}) as socket:
            assert socket.receive_json()["stream_id"] == frame["stream_id"]


@pytest.mark.parametrize(
    "headers", [{}, {"origin": "null"}, {"origin": "https://untrusted.example"}]
)
def test_websocket_rejects_missing_or_disallowed_origin(headers) -> None:
    with TestClient(create_app(Settings(provider="synthetic"))) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/api/ws", headers=headers):
                pass
        assert error.value.code == 1008


def test_websocket_connection_limit_and_client_messages() -> None:
    with TestClient(create_app(Settings(provider="synthetic", ws_max_clients=1))) as client:
        with client.websocket_connect("/api/ws", headers={"origin": ORIGIN}) as first:
            first.receive_json()
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect("/api/ws", headers={"origin": ORIGIN}):
                    pass
            assert error.value.code == 1013
            first.send_text("not a command endpoint")
            with pytest.raises(WebSocketDisconnect) as error:
                while True:
                    first.receive_json()
            assert error.value.code == 1008
        with client.websocket_connect("/api/ws", headers={"origin": ORIGIN}) as socket:
            assert socket.receive_json()["type"] == "snapshot"


@pytest.mark.anyio
async def test_slow_client_times_out_without_blocking_fast_client() -> None:
    settings = Settings(ws_send_timeout_seconds=0.1)
    service = IngestionService(SyntheticProvider(), settings)
    await service.poll_once()
    runtime = TelemetryRuntime(service)
    fast = runtime.subscribe()
    fast.get_nowait()
    connected = False
    blocked_send = asyncio.Event()
    events = []

    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await asyncio.Future()

    async def send(event):
        events.append(event)
        if event["type"] == "websocket.send":
            blocked_send.set()
            await asyncio.Future()

    socket = WebSocket(
        {
            "type": "websocket",
            "headers": [(b"origin", ORIGIN.encode())],
            "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
        },
        receive,
        send,
    )
    task = asyncio.create_task(aircraft_stream(socket))
    await asyncio.wait_for(blocked_send.wait(), 1)
    runtime.poller_status = "failed"
    runtime.snapshot()
    assert json.loads(fast.get_nowait())["poller_status"] == "failed"
    await asyncio.wait_for(task, 1)
    assert events[-1] == {"type": "websocket.close", "code": 1013, "reason": ""}
    assert runtime.subscriber_count == 1
    await runtime.stop()


@pytest.mark.anyio
async def test_websocket_task_cancellation_unsubscribes_before_returning() -> None:
    service = IngestionService(SyntheticProvider(), Settings())
    runtime = TelemetryRuntime(service)
    connected = asyncio.Event()
    held = asyncio.Event()

    async def receive():
        if not connected.is_set():
            connected.set()
            return {"type": "websocket.connect"}
        held.set()
        await asyncio.Future()

    async def send(_event):
        return None

    socket = WebSocket(
        {
            "type": "websocket",
            "headers": [(b"origin", ORIGIN.encode())],
            "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
        },
        receive,
        send,
    )
    task = asyncio.create_task(aircraft_stream(socket))
    await asyncio.wait_for(connected.wait(), 1)
    await asyncio.wait_for(held.wait(), 1)
    assert runtime.subscriber_count == 1
    task.cancel()
    await asyncio.wait_for(task, 1)
    assert runtime.subscriber_count == 0
    await runtime.stop()
