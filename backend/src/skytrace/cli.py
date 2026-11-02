"""One deliberate ingestion check; use --provider synthetic for offline data."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

from skytrace.config import Settings
from skytrace.providers.adsbfi import AdsbFiProvider
from skytrace.providers.adsblol import AdsbLolProvider
from skytrace.providers.base import AircraftProvider
from skytrace.providers.synthetic import SyntheticProvider
from skytrace.services.ingestion import IngestionService


async def check(settings: Settings, capture_dir: Path | None) -> int:
    # This context also closes cleanly on failure/cancellation. There are no client-owned retries.
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)
    ) as client:
        provider: AircraftProvider
        if settings.provider != "synthetic":
            live_provider = AdsbFiProvider if settings.provider == "adsbfi" else AdsbLolProvider
            provider = live_provider(
                client, max_snapshot_age_seconds=settings.max_snapshot_age_seconds
            )
        else:
            provider = SyntheticProvider()
        service = IngestionService(provider, settings)
        await service.poll_once()
        snapshot = service.snapshot()
        serialized = snapshot.model_dump_json(indent=2)
        print(serialized)
        if capture_dir is not None:
            (capture_dir / "normalized.json").write_text(serialized + "\n", encoding="utf-8")
            if isinstance(provider, AdsbLolProvider) and provider.last_response is not None:
                capture = provider.last_response
                (capture_dir / "response-body.raw").write_bytes(capture.body)
                metadata = {
                    "source": provider.attribution.model_dump(mode="json"),
                    "response": capture.metadata.model_dump(mode="json"),
                    "note": (
                        "Unmodified response body when read; "
                        "not a guarantee of valid or fresh data."
                    ),
                }
                (capture_dir / "response-metadata.json").write_text(
                    json.dumps(metadata, indent=2) + "\n",
                    encoding="utf-8",
                )
        return 0 if snapshot.status == "healthy" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=("synthetic", "adsblol", "adsbfi"), help="Override SKYTRACE_PROVIDER"
    )
    parser.add_argument("--capture-dir", type=Path, help="Save this check in a NEW local directory")
    args = parser.parse_args()
    try:
        settings = Settings(**({"provider": args.provider} if args.provider else {}))
        if args.capture_dir is not None:
            # Refuse to overwrite an existing capture, before making any HTTP request.
            args.capture_dir.mkdir(parents=True, exist_ok=False)
        exit_code = asyncio.run(check(settings, args.capture_dir))
    except (ValidationError, OSError) as error:
        print(f"Ingestion check failed: {error}", file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)
