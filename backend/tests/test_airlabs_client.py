"""Manufactured responses only. MockTransport is mandatory for every request."""

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from skytrace.services.airlabs_client import (
    BODY_LIMIT,
    URL,
    AirLabsClient,
    normalize_response,
    retry_after_seconds,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Real networking is forbidden in artificial AirLabs tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def artificial_response(**changes):
    row = dict(
        hex="ABC123",
        flight_icao=" zzz001 ",
        status="en-route",
        updated=int(NOW.timestamp()),
        dep_time_ts=int((NOW - timedelta(hours=1)).timestamp()),
        arr_time_ts=int((NOW + timedelta(hours=1)).timestamp()),
        dep_iata=None,
        dep_icao="kzzz",
        arr_iata="yyy",
        arr_icao=None,
    )
    row.update(changes)
    return {"response": row}


def normalized(**changes):
    return normalize_response(json.dumps(artificial_response(**changes)).encode())


def test_normalization_keeps_only_qualified_fields_and_dated_local_tuple():
    flight = normalized(registration="ARTIFICIAL", arbitrary={"secret": "discard"}).flight
    assert flight.hex == "abc123" and flight.flight_icao == "ZZZ001"
    assert flight.departure.iata is None and flight.departure.icao == "KZZZ"
    assert flight.arrival.iata == "YYY" and flight.arrival.icao is None
    assert not hasattr(flight, "registration") and not hasattr(flight, "arbitrary")
    later = normalized(
        dep_time_ts=int((NOW + timedelta(days=1)).timestamp()),
        arr_time_ts=int((NOW + timedelta(days=1, hours=2)).timestamp()),
    ).flight
    assert flight.local_leg != later.local_leg


@pytest.mark.parametrize("field", ["hex", "flight_icao", "updated", "dep_time_ts", "arr_time_ts"])
def test_missing_required_fields(field):
    assert normalized(**{field: None}).reason == "missing_fields"


@pytest.mark.parametrize(
    "value", [True, "1793188800", 1793188800.0, float("nan"), float("inf"), 10**100, -1, 0]
)
@pytest.mark.parametrize("field", ["updated", "dep_time_ts", "arr_time_ts"])
def test_strict_unix_fields(field, value):
    assert normalized(**{field: value}).flight is None


@pytest.mark.parametrize(
    "status", [None, "scheduled", "landed", "cancelled", "diverted", "En-route"]
)
def test_only_exact_en_route(status):
    assert normalized(status=status).reason == "status_unsupported"


@pytest.mark.parametrize(
    "changes",
    [
        {"hex": "~abc123"},
        {"hex": 123},
        {"hex": " abc123"},
        {"flight_icao": "ZZZ 001"},
        {"flight_icao": "ZZZ"},
        {"flight_icao": True},
        {"dep_iata": ""},
        {"dep_iata": "ZZ"},
        {"arr_icao": "K-YY"},
        {"dep_iata": None, "dep_icao": None},
        {"arr_iata": None, "arr_icao": None},
        {"arr_iata": "yıy"},
        {"dep_icao": "KZZZ "},
        {"dep_time_ts": int((NOW + timedelta(hours=2)).timestamp())},
    ],
)
def test_reject_malformed_supplied_codes_and_identity(changes):
    assert normalized(**changes).flight is None


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b'{"response":{},"response":{}}',
        b'{"extra":{"x":1,"x":2}}',
        b'{"extra":NaN}',
        b"[]",
        b'"text"',
        b'{"response":[{},{}]}',
    ],
)
def test_ambiguous_or_invalid_json(body):
    result = normalize_response(body)
    assert result.flight is None and result.reason in {"malformed", "invalid_fields"}


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("unknown_api_key", "authentication"),
        ("expired_api_key", "authentication"),
        ("minute_limit_exceeded", "rate_limited"),
        ("hour_limit_exceeded", "rate_limited"),
        ("month_limit_exceeded", "month_exhausted"),
        ("access_denied", "entitlement"),
        ("unrecognized", "source_error"),
        (999, "source_error"),
    ],
)
def test_http200_api_errors_never_become_success(code, reason):
    body = {
        "error": {"code": code, "message": "PRIVATE ARTIFICIAL MESSAGE"},
        **artificial_response(),
    }
    result = normalize_response(json.dumps(body).encode())
    assert result.reason == reason and result.flight is None
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 60),
        ("bad", 60),
        ("-1", 60),
        ("1.5", 60),
        ("0", 60),
        ("3600", 3600),
        ("99999999999999999999999999", 99999999999999999999999999),
        (format_datetime(NOW + timedelta(hours=3), usegmt=True), 10800),
        (format_datetime(NOW - timedelta(hours=3), usegmt=True), 60),
    ],
)
def test_retry_after(value, expected):
    assert retry_after_seconds(value, NOW) == expected


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.anyio
async def test_one_exact_query_no_redirect_retry_or_private_logging(caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/private"})

    client = AirLabsClient(SecretStr("ARTIFICIAL-KEY"), transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        result = await client.lookup("ZZZ001")
    await client.close()
    assert result.reason == "source_error" and len(requests) == 1
    request = requests[0]
    assert request.method == "GET" and str(request.url).split("?")[0] == URL
    assert dict(request.url.params) == {"api_key": "ARTIFICIAL-KEY", "flight_icao": "ZZZ001"}
    assert all(text not in caplog.text for text in ("ARTIFICIAL-KEY", "ZZZ001", URL))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limited"),
        (503, "source_error"),
    ],
)
async def test_http_failures_and_cooldown(status, reason):
    client = AirLabsClient(
        SecretStr("ARTIFICIAL"),
        clock=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"Retry-After": "999999999999"})
        ),
    )
    result = await client.lookup("ZZZ001")
    await client.close()
    assert result.reason == reason
    if status == 429:
        assert result.retry_after_seconds == 999999999999


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("headers", "chunks", "reason"),
    [
        ({"Content-Length": str(BODY_LIMIT + 1)}, [b"{}"], "body_too_large"),
        ({}, [b" " * 16384] * 17, "body_too_large"),
        ({"Content-Type": "text/html"}, [b"{}"], "malformed"),
        ({"Content-Encoding": "gzip"}, [b"compressed"], "malformed"),
        ({"Content-Length": "bad"}, [b"{}"], "malformed"),
        ({}, [b'{"response":null}'], "no_result"),
    ],
)
async def test_bounded_stream_closes_and_discards(headers, chunks, reason):
    stream = Chunks(chunks)
    client = AirLabsClient(
        SecretStr("ARTIFICIAL"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Type": "application/json", **headers}, stream=stream
            )
        ),
    )
    result = await client.lookup("ZZZ001")
    await client.close()
    assert result.reason == reason and stream.closed


@pytest.mark.anyio
async def test_timeout_has_no_retry_and_invalid_designator_spends_nothing(monkeypatch):
    import skytrace.services.airlabs_client as module

    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    client = AirLabsClient(SecretStr("ARTIFICIAL"), transport=httpx.MockTransport(handler))
    assert (await client.lookup("ZZZ 001")).reason == "invalid_callsign"
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 0.01)
    assert (await client.lookup("ZZZ001")).reason == "timeout"
    await client.close()
    assert calls == 1


@pytest.mark.anyio
async def test_success_and_transport_exception_never_return_raw_content():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=artificial_response())
        raise httpx.ConnectError("PRIVATE URL KEY BODY", request=request)

    client = AirLabsClient(SecretStr("ARTIFICIAL"), transport=httpx.MockTransport(handler))
    assert (await client.lookup("ZZZ001")).flight.hex == "abc123"
    result = await client.lookup("ZZZ001")
    await client.close()
    assert result.reason == "source_error" and "PRIVATE" not in repr(result)


@pytest.mark.anyio
async def test_response_cookies_are_not_retained_or_replayed():
    cookies = []

    def handler(request):
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json={"response": None},
            headers={"Set-Cookie": "provider_private=ARTIFICIAL; Path=/"},
        )

    client = AirLabsClient(SecretStr("ARTIFICIAL"), transport=httpx.MockTransport(handler))
    await client.lookup("ZZZ001")
    await client.lookup("ZZZ001")
    assert not client._client.cookies and cookies == [None, None]
    await client.close()
