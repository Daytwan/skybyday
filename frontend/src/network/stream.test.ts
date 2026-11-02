import assert from "node:assert/strict";
import test from "node:test";
import { fixture } from "./fixture.ts";
import type { Snapshot } from "./snapshot.ts";
import {
  type Scheduler,
  SnapshotStream,
  type SocketFactory,
} from "./stream.ts";

function harness() {
  const sockets: { handlers: Parameters<SocketFactory>[1]; closed: boolean }[] =
    [];
  const timers: { run: () => void; delay: number; cancelled: boolean }[] = [];
  const received: Snapshot[] = [];
  const statuses: string[] = [];
  const factory: SocketFactory = (_url, handlers) => {
    const socket = { handlers, closed: false };
    sockets.push(socket);
    return {
      close: () => {
        socket.closed = true;
      },
    };
  };
  const timer: Scheduler = (run, delay) => {
    const job = { run, delay, cancelled: false };
    timers.push(job);
    return () => {
      job.cancelled = true;
    };
  };
  const stream = new SnapshotStream(
    "ws://localhost/api/ws",
    (s) => received.push(s),
    (s) => statuses.push(s),
    factory,
    timer,
    () => 1,
  );
  const fire = () => {
    const job = timers.find((t) => !t.cancelled);
    assert.ok(job);
    job.cancelled = true;
    job.run();
  };
  const current = () => {
    const socket = sockets.at(-1);
    assert.ok(socket);
    return socket;
  };
  return { stream, sockets, timers, received, statuses, fire, current };
}

test("one connection per start, validated initial and subsequent snapshots, stop ignores late frames", () => {
  const h = harness();
  h.stream.start();
  h.stream.start();
  assert.equal(h.sockets.length, 1);
  h.current().handlers.open();
  assert.equal(h.statuses.at(-1), "Connected · awaiting snapshot");
  h.current().handlers.message(JSON.stringify(fixture()));
  assert.equal(h.received.length, 1);
  assert.equal(h.statuses.at(-1), "Connected");
  assert.ok(h.timers.every((t) => t.cancelled));
  h.stream.stop();
  h.current().handlers.message(JSON.stringify(fixture()));
  h.current().handlers.close();
  assert.equal(h.received.length, 1);
  assert.equal(h.current().closed, true);
  assert.ok(h.timers.every((t) => t.cancelled));
});

test("retries with bounded jittered backoff, resetting only after a valid snapshot", () => {
  const h = harness();
  h.stream.start();
  for (const expected of [1200, 2400, 4800, 9600, 19200, 30000, 30000]) {
    h.current().handlers.open();
    h.current().handlers.close();
    assert.equal(h.timers.find((t) => !t.cancelled)?.delay, expected);
    h.fire();
  }
  h.current().handlers.message(JSON.stringify(fixture()));
  h.current().handlers.close();
  assert.equal(h.timers.find((t) => !t.cancelled)?.delay, 1200);
  h.stream.stop();
});

test("initial snapshot timeout, malformed messages and obsolete connections cannot corrupt state", () => {
  const h = harness();
  h.stream.start();
  const original = h.current();
  assert.equal(h.timers[0]?.delay, 10000);
  h.fire();
  assert.equal(original.closed, true);
  h.fire();
  original.handlers.message(JSON.stringify(fixture()));
  assert.equal(h.received.length, 0);
  h.current().handlers.message("malformed JSON");
  assert.equal(h.received.length, 0);
  assert.equal(h.current().closed, true);
  assert.equal(h.timers.find((t) => !t.cancelled)?.delay, 2400);
  h.stream.stop();
  assert.ok(h.timers.every((t) => t.cancelled));
});
