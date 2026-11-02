"""Provider-independent observations: SI quantities, degrees and aware UTC times."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
Bearing = Annotated[float, Field(ge=0, lt=360)]
SourceName = Literal["adsblol", "adsbfi", "synthetic"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class TrackingArea(FrozenModel):
    latitude_deg: Latitude = 39.8561
    longitude_deg: Longitude = -104.6737
    radius_nm: int = Field(default=60, ge=1, le=250)


class AircraftObservation(FrozenModel):
    icao24: str = Field(pattern=r"^[0-9a-f]{6}$")
    source: SourceName
    source_type: str = Field(min_length=1, max_length=64)
    provider_timestamp: AwareDatetime
    received_at: AwareDatetime
    position_observed_at: AwareDatetime | None = None
    last_message_at: AwareDatetime | None = None
    callsign: str | None = Field(default=None, min_length=1, max_length=32)
    latitude_deg: Latitude | None = None
    longitude_deg: Longitude | None = None
    barometric_altitude_m: float | None = None
    geometric_altitude_m: float | None = None
    geometric_altitude_reference: Literal["wgs84_ellipsoid"] | None = None
    groundspeed_mps: float | None = Field(default=None, ge=0)
    ground_track_deg: Bearing | None = None
    true_heading_deg: Bearing | None = None
    magnetic_heading_deg: Bearing | None = None
    barometric_vertical_rate_mps: float | None = None
    geometric_vertical_rate_mps: float | None = None
    roll_deg: float | None = Field(default=None, ge=-180, le=180)
    pitch_deg: float | None = Field(default=None, ge=-90, le=90)
    aircraft_type: str | None = Field(default=None, min_length=1, max_length=32)
    on_ground: bool | None = None
    mlat_fields: tuple[str, ...] = ()
    tisb_fields: tuple[str, ...] = ()

    @field_validator("provider_timestamp", "received_at", "position_observed_at", "last_message_at")
    @classmethod
    def utc_times(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def coherent_observation(self) -> Self:
        position = (self.latitude_deg, self.longitude_deg, self.position_observed_at)
        if any(value is not None for value in position) and any(
            value is None for value in position
        ):
            raise ValueError("A position needs latitude, longitude and its observation time")
        for timestamp in (self.position_observed_at, self.last_message_at):
            if timestamp is not None and timestamp > self.provider_timestamp:
                raise ValueError("Observation time cannot follow the provider snapshot")
        if (self.geometric_altitude_m is None) != (self.geometric_altitude_reference is None):
            raise ValueError("Geometric altitude and its reference must occur together")
        return self


class SourceAttribution(FrozenModel):
    name: SourceName
    attribution: str
    url: str | None
    license: str
    license_url: str | None


class ResponseMetadata(FrozenModel):
    requested_url: str
    status_code: int
    received_at: AwareDatetime
    server_date: str | None = None
    age: str | None = None
    retry_after: str | None = None
    content_type: str | None = None
    cache_control: str | None = None


class RejectionCount(FrozenModel):
    reason: Literal["invalid_record", "non_icao_address"]
    count: int = Field(ge=1)


class BatchInfo(FrozenModel):
    source: SourceAttribution
    provider_timestamp: AwareDatetime
    received_at: AwareDatetime
    raw_record_count: int = Field(ge=0)
    rejected_record_count: int = Field(ge=0)
    rejections: tuple[RejectionCount, ...] = ()
    http: ResponseMetadata | None = None


class ProviderBatch(BatchInfo):
    aircraft: tuple[AircraftObservation, ...]

    @model_validator(mode="after")
    def coherent_batch(self) -> Self:
        if self.raw_record_count != len(self.aircraft) + self.rejected_record_count:
            raise ValueError("Raw, accepted and rejected counts must agree")
        if sum(reason.count for reason in self.rejections) != self.rejected_record_count:
            raise ValueError("Rejection reasons must account for rejected records")
        if any(plane.source != self.source.name for plane in self.aircraft):
            raise ValueError("A batch cannot silently mix providers")
        return self
