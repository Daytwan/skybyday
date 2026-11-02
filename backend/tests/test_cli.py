import json
from pathlib import Path

import httpx
import pytest

from skytrace.cli import main
from skytrace.models import ProviderBatch, TrackingArea
from skytrace.providers.adsblol import AdsbLolProvider
from skytrace.providers.base import ProviderError


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("CLI unit tests must not use the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def test_explicit_synthetic_cli_runs_offline_and_creates_labeled_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "capture"
    monkeypatch.setattr(
        "sys.argv", ["skytrace-ingest", "--provider", "synthetic", "--capture-dir", str(target)]
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "synthetic"
    assert payload["status"] == "healthy"
    assert len(payload["aircraft"]) == 2
    assert "not real aircraft" in payload["batch"]["source"]["attribution"]
    assert json.loads((target / "normalized.json").read_text()) == payload


def test_existing_capture_is_preserved_before_live_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("existing work")
    monkeypatch.setattr(
        "sys.argv", ["skytrace-ingest", "--provider", "adsblol", "--capture-dir", str(target)]
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert sentinel.read_text() == "existing work"


def test_default_live_cli_reports_retry_without_retrying_or_synthetic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    calls = 0

    async def throttled(self, area: TrackingArea) -> ProviderBatch:
        nonlocal calls
        calls += 1
        raise ProviderError("HTTP 429", retry_after_seconds=1800)

    monkeypatch.setattr(AdsbLolProvider, "fetch", throttled)
    monkeypatch.setattr("sys.argv", ["skytrace-ingest"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "degraded"
    assert payload["source"] == "adsblol"
    assert payload["aircraft"] == []
    assert payload["last_success_at"] is None
    assert 1799 < payload["retry_in_seconds"] <= 1800
    assert calls == 1
