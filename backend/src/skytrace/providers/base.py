"""One-attempt provider contract; scheduling belongs to the ingestion service."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from skytrace.models import ProviderBatch, SourceName, TrackingArea


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class AircraftProvider(ABC):
    source: SourceName

    @abstractmethod
    async def fetch(self, area: TrackingArea) -> ProviderBatch:
        """Perform at most one request, or return one synthetic observation batch."""
