"""Bounded latest observations, with authoritative successful snapshots."""

from datetime import datetime

from skytrace.models import AircraftObservation, ProviderBatch


def observation_time(plane: AircraftObservation) -> datetime:
    return plane.position_observed_at or plane.last_message_at or plane.provider_timestamp


def _prefer(current: AircraftObservation, incoming: AircraftObservation) -> AircraftObservation:
    if current.source != incoming.source:
        return incoming
    if current.position_observed_at is not None and incoming.position_observed_at is None:
        return current
    if incoming.position_observed_at is not None and current.position_observed_at is None:
        return incoming
    return incoming if observation_time(incoming) >= observation_time(current) else current


class AircraftCache:
    def __init__(self, *, max_aircraft: int, retention_seconds: float) -> None:
        self.max_aircraft = max_aircraft
        self.retention_seconds = retention_seconds
        self._states: dict[str, AircraftObservation] = {}
        self._scopes: dict[str, dict[str, AircraftObservation]] = {}
        self._scope_truncated: dict[str, int] = {}
        self._uses_scopes = False

    def _prune(self, now: datetime) -> None:
        for scope, observations in tuple(self._scopes.items()):
            retained = {
                identifier: plane
                for identifier, plane in observations.items()
                if (now - observation_time(plane)).total_seconds() < self.retention_seconds
            }
            if retained:
                self._scopes[scope] = retained
            else:
                del self._scopes[scope]
                self._scope_truncated.pop(scope, None)
        self._states = {
            identifier: plane
            for identifier, plane in self._states.items()
            if (now - observation_time(plane)).total_seconds() < self.retention_seconds
        }

    def _flatten_scopes(self) -> dict[str, AircraftObservation]:
        flattened: dict[str, AircraftObservation] = {}
        for observations in self._scopes.values():
            for identifier, incoming in observations.items():
                current = flattened.get(identifier)
                flattened[identifier] = _prefer(current, incoming) if current else incoming
        return flattened

    def _bounded(
        self, observations: dict[str, AircraftObservation]
    ) -> dict[str, AircraftObservation]:
        freshest = sorted(
            observations.values(),
            key=lambda plane: (-observation_time(plane).timestamp(), plane.icao24),
        )[: self.max_aircraft]
        return {plane.icao24: plane for plane in freshest}

    def _bound(self, observations: dict[str, AircraftObservation]) -> None:
        self._states = self._bounded(observations)

    def replace(self, batch: ProviderBatch, now: datetime) -> None:
        self._prune(now)
        replacement: dict[str, AircraftObservation] = {}
        for incoming in batch.aircraft:
            current = replacement.get(incoming.icao24, self._states.get(incoming.icao24))
            replacement[incoming.icao24] = _prefer(current, incoming) if current else incoming
        # A successful empty result clears the cache. Errors never call replace.
        self._scopes.clear()
        self._scope_truncated.clear()
        self._uses_scopes = False
        self._bound(replacement)
        self._prune(now)

    def replace_scope(self, scope: str, batch: ProviderBatch, now: datetime) -> None:
        """Atomically replace one circle's authoritative membership.

        Other circles keep their last successful observations, so a rotation never
        makes an unrelated area disappear.  The flattened public cache remains
        bounded by ``max_aircraft``.
        """
        self._prune(now)
        previous = self._scopes.get(scope, {})
        replacement: dict[str, AircraftObservation] = {}
        for incoming in batch.aircraft:
            current = replacement.get(incoming.icao24, previous.get(incoming.icao24))
            replacement[incoming.icao24] = _prefer(current, incoming) if current else incoming
        bounded = self._bounded(replacement)
        self._scopes[scope] = bounded
        self._scope_truncated[scope] = len(replacement) - len(bounded)
        self._uses_scopes = True
        self._bound(self._flatten_scopes())
        self._prune(now)

    def states(self, now: datetime) -> tuple[AircraftObservation, ...]:
        # Reads expire data during a long Retry-After pause without any network I/O.
        self._prune(now)
        if self._uses_scopes:
            # Scope expiry or a later overlap must never leave a prior flattened
            # member visible. Rebuild this bounded public projection on reads.
            self._bound(self._flatten_scopes())
        return tuple(self._states[identifier] for identifier in sorted(self._states))

    def collection_counts(self, now: datetime) -> tuple[int, int, int, int]:
        """Return scope membership, published rows, truncations and capacity."""
        self.states(now)
        scope_members = sum(len(observations) for observations in self._scopes.values())
        published = len(self._states)
        scope_truncated = sum(self._scope_truncated.values())
        flattened = len(self._flatten_scopes()) if self._uses_scopes else published
        return (
            scope_members,
            published,
            scope_truncated + max(0, flattened - published),
            self.max_aircraft,
        )
