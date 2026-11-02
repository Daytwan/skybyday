"""Cache-only HTTP routes and a receive-only WebSocket snapshot protocol."""

import asyncio
import json
import math
from datetime import datetime, timedelta
from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, WebSocket
from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from skytrace.history import (
    AircraftHistoryResponse,
    HistoryStatus,
    HistoryStore,
    HistoryUnavailable,
    PositionHistoryResponse,
)
from skytrace.models import AircraftObservation, SourceAttribution, SourceName, TrackingArea
from skytrace.services.airlabs_models import (
    FlightContextBatch,
    FlightContextCommand,
    FlightContextIntent,
    FlightContextRejection,
    admit_intent,
)
from skytrace.services.ingestion import ProviderStatus
from skytrace.services.routes import RouteResponse
from skytrace.services.runtime import SnapshotMessage, Subscription, TelemetryRuntime
from skytrace.services.weather import WeatherResponse

router = APIRouter(prefix="/api")


@router.get("/weather", response_model=WeatherResponse, tags=["weather"])
async def weather(request: Request, response: Response) -> WeatherResponse:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.weather.get()


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["skytrace"] = "skytrace"
    version: str
    milestone: Literal["backend-api"] = "backend-api"
    telemetry: SourceName
    provider_status: ProviderStatus
    poller_running: bool
    cached_aircraft_count: int
    area: TrackingArea
    poll_interval_seconds: float


class AircraftDetailResponse(BaseModel):
    schema_version: Literal[1] = 1
    stream_id: str
    sequence: int
    emitted_at: AwareDatetime
    source: SourceAttribution
    provider_status: ProviderStatus
    stale: bool
    aircraft: AircraftObservation


class FleetRouteReference(BaseModel):
    icao24: str
    route: RouteResponse


class FleetRouteResponse(BaseModel):
    schema_version: Literal[1] = 1
    stream_id: str
    sequence: int
    emitted_at: AwareDatetime
    source_ready: bool
    tracked_aircraft: int
    two_endpoint_references: int
    multi_leg_references: int
    unknown: int
    unavailable: int
    confirmed_current_flights: Literal[0] = 0
    references: list[FleetRouteReference] = Field(max_length=10_000)


def runtime_for(connection: HTTPConnection) -> TelemetryRuntime:
    runtime = getattr(connection.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(503, "Telemetry lifecycle has not started")
    return runtime


def history_for(connection: HTTPConnection) -> HistoryStore:
    history = getattr(connection.app.state, "history", None)
    if history is None:
        raise HTTPException(503, "Trajectory history is unavailable")
    return history


def history_window(start: datetime, end: datetime) -> None:
    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0):
        raise HTTPException(422, "History timestamps must be UTC")
    if start >= end:
        raise HTTPException(422, "History start must precede end")
    if end - start > timedelta(hours=24):
        raise HTTPException(422, "History windows cannot exceed 24 hours")


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request, response: Response) -> HealthResponse:
    runtime = runtime_for(request)
    message = runtime.snapshot()
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        version=version("skytrace"),
        telemetry=message.data.source,
        provider_status=message.data.status,
        poller_running=message.poller_status == "running",
        cached_aircraft_count=len(message.data.aircraft),
        area=runtime.ingestion.area,
        poll_interval_seconds=runtime.ingestion.settings.poll_interval_seconds,
    )


@router.get("/aircraft", response_model=SnapshotMessage, tags=["aircraft"])
async def aircraft_list(request: Request, response: Response) -> SnapshotMessage:
    response.headers["Cache-Control"] = "no-store"
    return runtime_for(request).snapshot()


@router.get("/routes", response_model=FleetRouteResponse, tags=["aircraft"])
async def fleet_routes(request: Request, response: Response) -> FleetRouteResponse:
    """Resolve every cached callsign locally; never spend the upstream request budget."""
    response.headers["Cache-Control"] = "no-store"
    message = runtime_for(request).snapshot()
    service = request.app.state.routes
    references = [
        FleetRouteReference(
            icao24=plane.icao24,
            route=service.resolve_reference(plane.callsign)
            if plane.source in {"adsblol", "adsbfi"}
            else RouteResponse(callsign=plane.callsign, status="unknown"),
        )
        for plane in message.data.aircraft
    ]
    return FleetRouteResponse(
        stream_id=message.stream_id,
        sequence=message.sequence,
        emitted_at=message.data.emitted_at,
        source_ready=bool(service.reference_index.source_commit),
        tracked_aircraft=len(references),
        two_endpoint_references=sum(
            item.route.status == "reference" and len(item.route.airports) == 2
            for item in references
        ),
        multi_leg_references=sum(
            item.route.status == "reference" and len(item.route.airports) > 2 for item in references
        ),
        unknown=sum(item.route.status == "unknown" for item in references),
        unavailable=sum(item.route.status == "unavailable" for item in references),
        references=references,
    )


@router.get("/aircraft/{icao24}", response_model=AircraftDetailResponse, tags=["aircraft"])
async def aircraft_detail(
    request: Request,
    response: Response,
    icao24: Annotated[str, Path(pattern=r"^[0-9a-f]{6}$")],
) -> AircraftDetailResponse:
    runtime = runtime_for(request)
    message = runtime.snapshot()
    for plane in message.data.aircraft:
        if plane.icao24 == icao24:
            assert message.data.batch is not None
            response.headers["Cache-Control"] = "no-store"
            return AircraftDetailResponse(
                stream_id=message.stream_id,
                sequence=message.sequence,
                emitted_at=message.data.emitted_at,
                source=message.data.batch.source,
                provider_status=message.data.status,
                stale=plane.position_observed_at is None
                or (message.data.emitted_at - plane.position_observed_at).total_seconds()
                >= runtime.ingestion.settings.stale_after_seconds,
                aircraft=plane,
            )
    raise HTTPException(
        404, "Aircraft is not in the current cache", headers={"Cache-Control": "no-store"}
    )


@router.get("/aircraft/{icao24}/route", response_model=RouteResponse, tags=["aircraft"])
async def aircraft_route(
    request: Request,
    response: Response,
    icao24: Annotated[str, Path(pattern=r"^[0-9a-f]{6}$")],
) -> RouteResponse:
    response.headers["Cache-Control"] = "no-store"
    for plane in runtime_for(request).snapshot().data.aircraft:
        if plane.icao24 == icao24:
            if plane.source not in {"adsblol", "adsbfi"}:
                return RouteResponse(callsign=plane.callsign, status="unknown")
            if plane.source == "adsbfi":
                return request.app.state.routes.resolve_reference(plane.callsign)
            return await request.app.state.routes.get(plane.callsign)
    raise HTTPException(404, "Aircraft is not in the current cache")


@router.get("/history/status", response_model=HistoryStatus, tags=["history"])
async def history_status(request: Request, response: Response) -> HistoryStatus:
    response.headers["Cache-Control"] = "no-store"
    return history_for(request).status()


@router.get(
    "/history/aircraft/{icao24}",
    response_model=AircraftHistoryResponse,
    tags=["history"],
)
async def aircraft_history(
    request: Request,
    response: Response,
    icao24: Annotated[str, Path(pattern=r"^[0-9a-f]{6}$")],
    source: Annotated[SourceName, Query()],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> AircraftHistoryResponse:
    history_window(start, end)
    try:
        result = await history_for(request).aircraft(source, icao24, start, end, limit)
    except HistoryUnavailable as error:
        raise HTTPException(503, "Trajectory history is unavailable") from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/history/positions", response_model=PositionHistoryResponse, tags=["history"])
async def position_history(
    request: Request,
    response: Response,
    source: Annotated[SourceName, Query()],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    latitude_deg: Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False)],
    longitude_deg: Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False)],
    radius_m: Annotated[float, Query(gt=0, le=500_000, allow_inf_nan=False)],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> PositionHistoryResponse:
    history_window(start, end)
    try:
        result = await history_for(request).positions(
            source, start, end, latitude_deg, longitude_deg, radius_m, limit
        )
    except HistoryUnavailable as error:
        raise HTTPException(503, "Trajectory history is unavailable") from error
    response.headers["Cache-Control"] = "no-store"
    return result


async def _send_snapshots(websocket: WebSocket, queue: Subscription, timeout: float) -> int:
    while (message := await queue.get()) is not None:
        try:
            async with asyncio.timeout(timeout):
                await websocket.send_text(message)
        except TimeoutError:
            return 1013
    return 1001


async def _receive_disconnect(websocket: WebSocket) -> int:
    event = await websocket.receive()
    return 1000 if event["type"] == "websocket.disconnect" else 1008


@router.websocket("/ws")
async def aircraft_stream(websocket: WebSocket) -> None:
    runtime = getattr(websocket.app.state, "runtime", None)
    if runtime is None:
        await websocket.close(code=1013)
        return
    settings = runtime.ingestion.settings
    origins = websocket.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in settings.ws_allowed_origins:
        await websocket.close(code=1008)
        return
    queue = runtime.subscribe()
    if queue is None:
        await websocket.close(code=1013)
        return
    tasks: set[asyncio.Task[int]] = set()
    close_code = 1001
    try:
        async with asyncio.timeout(settings.ws_send_timeout_seconds):
            await websocket.accept()
        tasks = {
            asyncio.create_task(
                _send_snapshots(websocket, queue, settings.ws_send_timeout_seconds)
            ),
            asyncio.create_task(_receive_disconnect(websocket)),
        }
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            close_code = task.result()
    except (WebSocketDisconnect, OSError, TimeoutError, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        # TestClient (and a real peer that vanishes during its close handshake)
        # can cancel this handler while its child tasks are unwinding.  Cleanup
        # still has to release the runtime subscription before returning.
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        runtime.unsubscribe(queue)
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                async with asyncio.timeout(settings.ws_send_timeout_seconds):
                    await websocket.close(code=close_code)
            except (WebSocketDisconnect, OSError, TimeoutError, asyncio.CancelledError):
                pass


# Admission order: origin/media/intent headers (403), bounded JSON/schema (422),
# lifecycle (503), current identity/view/freshness (409 or 422), service gate (503).
# No request rejection invokes submit; a cache-only GET invokes only project.
def context_error(error: FlightContextRejection, snapshot=None) -> JSONResponse:
    retry = getattr(error, "retry_in_seconds", None)
    if isinstance(retry, bool) or not isinstance(retry, (int, float)):
        retry = None
    else:
        try:
            if not math.isfinite(retry) or retry < 0:
                retry = None
        except (OverflowError, ValueError):
            retry = None
    headers = {"Cache-Control": "no-store"}
    if retry is not None:
        headers["Retry-After"] = str(math.ceil(retry))
    payload = FlightContextCommand(
        stream_id=snapshot.stream_id if snapshot is not None else None,
        sequence=snapshot.sequence if snapshot is not None else None,
        state="unavailable" if error.status == 503 else "ineligible",
        reason=error.reason,
        retry_in_seconds=retry,
    )
    return JSONResponse(
        status_code=error.status,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def require_context_headers(request: Request) -> None:
    settings = request.app.state.settings
    origins = request.headers.getlist("origin")
    content_types = request.headers.getlist("content-type")
    intents = request.headers.getlist("x-skytrace-flight-intent")
    sites = request.headers.getlist("sec-fetch-site")
    if (
        len(origins) != 1
        or origins[0] not in settings.ws_allowed_origins
        or len(content_types) != 1
        or intents != ["lookup"]
        or len(sites) > 1
        or any(site.lower() == "cross-site" for site in sites)
    ):
        raise FlightContextRejection(403, "origin_rejected")
    media = [part.strip().lower() for part in content_types[0].split(";")]
    if media not in (["application/json"], ["application/json", "charset=utf-8"]):
        raise FlightContextRejection(403, "origin_rejected")


async def parse_context_intent(request: Request) -> FlightContextIntent:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2048:
            raise FlightContextRejection(422, "body_too_large")
        body.extend(chunk)

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def reject_constant(value):
        raise ValueError("nonfinite number")

    try:
        # Strict UTF-8 and bounded parse first; DTO validators parse ISO UTC
        # timestamps while preserving strict integer and boolean checks.
        raw = body.decode("utf-8")
        json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        return FlightContextIntent.model_validate_json(raw)
    except (ValueError, ValidationError, RecursionError):
        raise FlightContextRejection(422, "invalid_request") from None


@router.get("/flight-contexts", response_model=FlightContextBatch, tags=["aircraft"])
async def flight_contexts(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    runtime = getattr(request.app.state, "runtime", None)
    service = getattr(request.app.state, "flight_contexts", None)
    if runtime is None or service is None:
        return context_error(FlightContextRejection(503, "lifecycle_unavailable"))
    return service.project(runtime.snapshot())


@router.post("/flight-contexts", response_model=FlightContextCommand, tags=["aircraft"])
async def flight_context_intent(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    snapshot = None
    try:
        require_context_headers(request)
        intent = await parse_context_intent(request)
        runtime = getattr(request.app.state, "runtime", None)
        service = getattr(request.app.state, "flight_contexts", None)
        if runtime is None or service is None:
            raise FlightContextRejection(503, "lifecycle_unavailable")
        snapshot = runtime.snapshot()
        admit_intent(intent, snapshot, runtime.ingestion.clock())
        result = service.submit(intent, snapshot)
        response.status_code = 202 if result.state in ("queued", "pending") else 200
        return result
    except FlightContextRejection as error:
        return context_error(error, snapshot)
