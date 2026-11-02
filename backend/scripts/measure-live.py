"""Measure 10 local WebSocket viewers against one live backend, then shut it down.

Run from the repo root with port 8000 free:
uv run --project backend --locked python backend/scripts/measure-live.py
Linux /proc measurements exclude browsers, imagery, history storage and other apps.
This makes normal live provider requests; do not repeatedly restart it during throttling.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data/qa"


def measure(process: subprocess.Popen) -> tuple[float, float]:
    stat = Path(f"/proc/{process.pid}/stat").read_text().split()
    status = Path(f"/proc/{process.pid}/status").read_text().splitlines()
    rss = int(next(x.split()[1] for x in status if x.startswith("VmRSS:")))
    return rss / 1024, (int(stat[13]) + int(stat[14])) / os.sysconf("SC_CLK_TCK")


async def benchmark(process: subprocess.Popen) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("Backend failed; check capacity-backend.log and port 8000")
            try:
                response = await client.get("http://127.0.0.1:8000/api/health", timeout=1)
                response.raise_for_status()
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Backend startup timed out")
    await asyncio.sleep(3)
    sockets = await asyncio.gather(
        *(
            websockets.connect("ws://127.0.0.1:8000/api/ws", origin="http://127.0.0.1:4173")
            for _ in range(10)
        )
    )
    received, messages, counts, successes = [0] * 10, [0] * 10, [], set()

    async def consume(index, websocket):
        async for message in websocket:
            received[index] += len(message.encode())
            messages[index] += 1
            data = json.loads(message)["data"]
            if data["batch"]:
                assert data["batch"]["source"]["name"] == "adsblol"
                successes.add(data["last_success_at"])
                counts.append(len(data["aircraft"]))

    tasks = [asyncio.create_task(consume(i, ws)) for i, ws in enumerate(sockets)]
    rss, cpu = measure(process)
    started = time.monotonic()
    samples = [rss]
    for _ in range(65):
        await asyncio.sleep(1)
        samples.append(measure(process)[0])
    elapsed = time.monotonic() - started
    _, end_cpu = measure(process)
    await asyncio.gather(*(ws.close() for ws in sockets))
    await asyncio.gather(*tasks)
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "clients": 10,
        "seconds": round(elapsed, 2),
        "aircraft_count_range": [min(counts), max(counts)] if counts else None,
        "backend_rss_mib_min": round(min(samples), 2),
        "backend_rss_mib_max": round(max(samples), 2),
        "cpu_seconds": round(end_cpu - cpu, 3),
        "single_core_percent": round((end_cpu - cpu) / elapsed * 100, 2),
        "received_bytes_all_clients": sum(received),
        "bytes_per_second_all_clients": round(sum(received) / elapsed),
        "messages_per_client": messages,
        "unique_successful_snapshot_times": len(successes),
        "limits": (
            "Regional live traffic; history off. Excludes browsers, imagery, "
            "VPS conditions and student workload."
        ),
    }
    process.send_signal(signal.SIGTERM)
    await asyncio.to_thread(process.wait, 10)
    # Uvicorn re-raises SIGTERM after completing its lifespan shutdown.
    report["process_return_code"] = process.returncode
    report["application_shutdown_complete"] = (
        "Application shutdown complete" in (OUTPUT / "capacity-backend.log").read_text()
    )
    (OUTPUT / "capacity.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / "capacity-backend.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "skytrace.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--workers",
                "1",
                "--ws",
                "websockets-sansio",
                "--ws-max-size",
                "1024",
                "--timeout-graceful-shutdown",
                "5",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            asyncio.run(benchmark(process))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
