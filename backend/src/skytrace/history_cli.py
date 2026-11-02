"""Explicit management commands for optional PostGIS trajectory history."""

import argparse
import asyncio
import sys

from pydantic import ValidationError

from skytrace.config import Settings
from skytrace.history import HistoryUnavailable, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate",))
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.command == "migrate":
            asyncio.run(migrate(settings))
    except (HistoryUnavailable, ValidationError) as error:
        print(f"History command failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
