# Synthetic snapshot contract

`snapshot.json` was serialized by `TelemetryRuntime.snapshot().model_dump_json()`
with the synthetic provider, default settings, a fixed UTC clock
(`2026-10-19T12:00:00Z`), monotonic clock `0`, and a fixed stream UUID. It contains
fictional aircraft only. The frontend tests validate this backend-produced
contract and derive invalid, outage, and lifecycle cases from it.
