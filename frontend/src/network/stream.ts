import { decodeSnapshot, type Snapshot } from "./snapshot.ts";

interface Handlers {
  open: () => void;
  message: (data: unknown) => void;
  close: () => void;
}
export type SocketFactory = (
  url: string,
  handlers: Handlers,
) => { close: () => void };
export type Scheduler = (
  callback: () => void,
  milliseconds: number,
) => () => void;
const openSocket: SocketFactory = (url, handlers) => {
  const socket = new WebSocket(url);
  socket.addEventListener("open", handlers.open);
  socket.addEventListener("message", (event) => handlers.message(event.data));
  socket.addEventListener("close", handlers.close);
  socket.addEventListener("error", () => socket.close());
  return { close: () => socket.close() };
};
const schedule: Scheduler = (callback, delay) => {
  const id = setTimeout(callback, delay);
  return () => clearTimeout(id);
};

export class SnapshotStream {
  private socket: ReturnType<SocketFactory> | null = null;
  private cancelTimer: (() => void) | null = null;
  private generation = 0;
  private failures = 0;
  private stopped = true;
  private readonly url: string;
  private readonly onSnapshot: (snapshot: Snapshot) => void;
  private readonly onStatus: (status: string) => void;
  private readonly factory: SocketFactory;
  private readonly timer: Scheduler;
  private readonly random: () => number;

  constructor(
    url: string,
    onSnapshot: (snapshot: Snapshot) => void,
    onStatus: (status: string) => void,
    factory: SocketFactory = openSocket,
    timer: Scheduler = schedule,
    random: () => number = Math.random,
  ) {
    this.url = url;
    this.onSnapshot = onSnapshot;
    this.onStatus = onStatus;
    this.factory = factory;
    this.timer = timer;
    this.random = random;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }
  stop(): void {
    this.stopped = true;
    this.generation += 1;
    this.cancelTimer?.();
    this.cancelTimer = null;
    this.socket?.close();
    this.socket = null;
  }
  private connect(): void {
    const generation = ++this.generation;
    let cancelHandshake: (() => void) | null = null;
    const active = () => !this.stopped && generation === this.generation;
    const disconnected = () => {
      if (!active()) return;
      this.generation += 1;
      cancelHandshake?.();
      this.socket?.close();
      this.socket = null;
      const base = Math.min(30_000, 1000 * 2 ** Math.min(this.failures++, 5));
      const delay = Math.min(30_000, base * (1 + this.random() * 0.2));
      this.onStatus(`Disconnected · retry in ${Math.ceil(delay / 1000)}s`);
      this.cancelTimer = this.timer(() => {
        this.cancelTimer = null;
        this.connect();
      }, delay);
    };
    this.onStatus("Connecting…");
    try {
      this.socket = this.factory(this.url, {
        open: () => {
          if (active()) this.onStatus("Connected · awaiting snapshot");
        },
        message: (data) => {
          if (!active()) return;
          let snapshot: Snapshot;
          try {
            snapshot = decodeSnapshot(data);
          } catch {
            this.onStatus("Invalid snapshot · reconnecting");
            disconnected();
            return;
          }
          cancelHandshake?.();
          cancelHandshake = null;
          this.cancelTimer = null;
          this.failures = 0;
          this.onSnapshot(snapshot);
          this.onStatus("Connected");
        },
        close: disconnected,
      });
      cancelHandshake = this.timer(disconnected, 10_000);
      this.cancelTimer = cancelHandshake;
    } catch {
      disconnected();
    }
  }
}
