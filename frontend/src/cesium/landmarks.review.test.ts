import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import {
  ArcGISTiledElevationTerrainProvider,
  Cartesian3,
  Ellipsoid,
  Event,
  GeoJsonDataSource,
  I3SDataProvider,
  Ray,
  Rectangle,
  type Viewer,
} from "cesium";
import { LandmarksLayer } from "./landmarks.ts";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function eventually(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 20 && !predicate(); attempt++)
    await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(predicate(), true);
}

function fakeViewer(): {
  viewer: Viewer;
  primitives: object[];
  removed: object[];
  triggerMoveEnd: () => void;
  renderFrame: (duringRender?: () => void) => void;
} {
  const primitives: object[] = [];
  const removed: object[] = [];
  let moveEndListener: (() => void) | undefined;
  const preRender = new Event();
  const postRender = new Event();
  const camera = {
    positionWC: Cartesian3.fromDegrees(-104.9896, 39.7397, 5_000),
    computeViewRectangle: () =>
      Rectangle.fromDegrees(-105.01, 39.73, -104.97, 39.76),
    getPickRay: () => undefined,
    moveEnd: {
      addEventListener: (listener: () => void) => {
        moveEndListener = listener;
        return () => {
          if (moveEndListener === listener) moveEndListener = undefined;
        };
      },
    },
  };
  const viewer = {
    camera,
    clock: { currentTime: 0 },
    scene: {
      preRender,
      postRender,
      canvas: { clientWidth: 1000, clientHeight: 800 },
      globe: { ellipsoid: Ellipsoid.WGS84, pick: () => undefined },
      primitives: {
        add(value: object) {
          primitives.push(value);
          return value;
        },
        remove(value: object) {
          removed.push(value);
          const index = primitives.indexOf(value);
          if (index >= 0) primitives.splice(index, 1);
          (value as { destroy?: () => void }).destroy?.();
          return index >= 0;
        },
      },
      requestRender() {},
    },
    dataSources: {
      add: async (value: object) => value,
      remove: () => true,
    },
    cesiumWidget: { creditDisplay: { addStaticCredit() {} } },
  } as unknown as Viewer;
  return {
    viewer,
    primitives,
    removed,
    triggerMoveEnd: () => moveEndListener?.(),
    renderFrame: (duringRender) => {
      preRender.raiseEvent();
      duringRender?.();
      postRender.raiseEvent();
    },
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function installLocalLandmarkStubs(
  t: TestContext,
  manifest: unknown = {
    version: 1,
    building_count: 0,
    chunk_degrees: 0.01,
    vertical_reference: "NAVD88(GEOID12A) orthometric heights",
    chunks: {},
  },
  fetchOverride?: (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => Promise<Response>,
): void {
  t.mock.method(
    GeoJsonDataSource,
    "load",
    async () =>
      ({
        entities: { values: [] },
        show: false,
      }) as unknown as GeoJsonDataSource,
  );
  t.mock.method(
    globalThis,
    "fetch",
    fetchOverride ?? (async () => jsonResponse(manifest)),
  );
  t.mock.method(
    ArcGISTiledElevationTerrainProvider,
    "fromUrl",
    async () => ({}) as ArcGISTiledElevationTerrainProvider,
  );
}

function remoteProvider(): {
  show: boolean;
  destroyCount: number;
  destroy(): void;
  layers: { tileset: { tileVisible: Event; tileFailed: Event } }[];
} {
  const provider = {
    show: false,
    destroyCount: 0,
    layers: [
      { tileset: { tileVisible: new Event(), tileFailed: new Event() } },
    ],
    destroy() {
      provider.destroyCount += 1;
    },
  };
  return provider;
}

test("does not duplicate a pending photogrammetry load across offline to online", async (t) => {
  installLocalLandmarkStubs(t);
  const pending = deferred<ReturnType<typeof remoteProvider>>();
  let calls = 0;
  t.mock.method(I3SDataProvider, "fromUrl", async () => {
    calls += 1;
    return pending.promise as unknown as I3SDataProvider;
  });
  const { viewer, primitives } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});

  await layer.setEnabled(true, true);
  await eventually(() => calls === 1);
  await layer.setEnabled(true, false);
  await layer.setEnabled(true, true);
  assert.equal(calls, 1);

  const provider = remoteProvider();
  pending.resolve(provider);
  await eventually(() => primitives.includes(provider));
  assert.equal(provider.destroyCount, 0);
  layer.destroy();
  assert.equal(provider.destroyCount, 1);
});

test("destroys a stale provider that resolves after remote layers are disabled", async (t) => {
  installLocalLandmarkStubs(t);
  const pending = deferred<ReturnType<typeof remoteProvider>>();
  let calls = 0;
  t.mock.method(I3SDataProvider, "fromUrl", async () => {
    calls += 1;
    return pending.promise as unknown as I3SDataProvider;
  });
  const { viewer, primitives } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});

  await layer.setEnabled(true, true);
  await eventually(() => calls === 1);
  await layer.setEnabled(true, false);

  const provider = remoteProvider();
  pending.resolve(provider);
  await eventually(() => provider.destroyCount === 1);
  assert.equal(primitives.includes(provider), false);
  layer.destroy();
});

function buildingFixture() {
  const key = "12973-7500";
  const manifest = {
    version: 1,
    building_count: 1,
    chunk_degrees: 0.01,
    vertical_reference: "NAVD88(GEOID12A) orthometric heights",
    chunks: {
      [key]: {
        count: 1,
        bytes: 512,
        sha256: "a".repeat(64),
        bounds: [-105, 39.73, -104.99, 39.74],
      },
    },
  };
  const chunk = {
    version: 1,
    buildings: [
      [
        1,
        10,
        1600,
        "commercial",
        [
          [
            [
              [-104.995, 39.735],
              [-104.994, 39.735],
              [-104.994, 39.736],
              [-104.995, 39.736],
              [-104.995, 39.735],
            ],
          ],
        ],
      ],
    ],
  };
  return { key, manifest, chunk };
}

test("deduplicates pending building chunks and removes every primitive on destroy", async (t) => {
  const { key, manifest, chunk } = buildingFixture();
  const pendingChunk = deferred<Response>();
  let manifestCalls = 0;
  let chunkCalls = 0;
  installLocalLandmarkStubs(t, manifest, async (input) => {
    const url = String(input);
    if (url.endsWith("/manifest.json")) {
      manifestCalls += 1;
      return jsonResponse(manifest);
    }
    if (url.endsWith(`/${key}.json`)) {
      chunkCalls += 1;
      return pendingChunk.promise;
    }
    throw new Error(`Unexpected landmark request: ${url}`);
  });
  const { viewer, primitives, removed, triggerMoveEnd } = fakeViewer();
  const reports: string[] = [];
  const layer = new LandmarksLayer(viewer, (text) => reports.push(text));

  await layer.setEnabled(true, false);
  await eventually(() => manifestCalls === 1);
  await layer.setEnabled(true, true);
  await eventually(() => chunkCalls === 1);

  const updates = Array.from({ length: 4 }, () => layer.setEnabled(true, true));
  triggerMoveEnd();
  triggerMoveEnd();
  triggerMoveEnd();
  await Promise.all(updates);
  await Promise.resolve();
  assert.equal(chunkCalls, 1);

  pendingChunk.resolve(jsonResponse(chunk));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(primitives.length, 1, reports.join(" | "));

  layer.destroy();
  assert.equal(primitives.length, 0);
  assert.equal(removed.length, 1);
});

test("landmarks off destroys loaded mesh and buildings; re-enabling loads fresh resources", async (t) => {
  const { manifest, chunk } = buildingFixture();
  installLocalLandmarkStubs(t, manifest, async (input) =>
    jsonResponse(String(input).endsWith("/manifest.json") ? manifest : chunk),
  );
  const providers: ReturnType<typeof remoteProvider>[] = [];
  t.mock.method(I3SDataProvider, "fromUrl", async () => {
    const provider = remoteProvider();
    providers.push(provider);
    return provider as unknown as I3SDataProvider;
  });
  const { viewer, primitives, removed } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});
  t.after(() => layer.destroy());

  await layer.setEnabled(true);
  await eventually(() => primitives.length === 2);
  const firstResources = [...primitives];
  await layer.setEnabled(false);
  assert.equal(primitives.length, 0);
  assert.equal(providers[0]?.destroyCount, 1);
  assert.deepEqual(new Set(removed), new Set(firstResources));

  await layer.setEnabled(true);
  await eventually(() => primitives.length === 2);
  assert.equal(providers.length, 2);
  assert.ok(primitives.every((value) => !firstResources.includes(value)));
  layer.destroy();
  assert.equal(primitives.length, 0);
  assert.ok(providers.every((provider) => provider.destroyCount === 1));
});

test("landmarks off disposes a late mesh and preserves paused status", async (t) => {
  installLocalLandmarkStubs(t);
  const pending = deferred<ReturnType<typeof remoteProvider>>();
  const load = t.mock.method(
    I3SDataProvider,
    "fromUrl",
    () => pending.promise as unknown as Promise<I3SDataProvider>,
  );
  const { viewer, primitives } = fakeViewer();
  const reports: string[] = [];
  const layer = new LandmarksLayer(viewer, (message) => reports.push(message));
  t.after(() => layer.destroy());

  await layer.setEnabled(true);
  await eventually(() => load.mock.callCount() === 1);
  await layer.setEnabled(false);
  const provider = remoteProvider();
  pending.resolve(provider);
  await eventually(() => provider.destroyCount === 1);
  assert.equal(primitives.length, 0);
  assert.equal(reports.at(-1), "Denver measured buildings paused");
});

test("disabling during geoid setup prevents the next remote loading stage", async (t) => {
  installLocalLandmarkStubs(t);
  const pending = deferred<ArcGISTiledElevationTerrainProvider>();
  const geoid = t.mock.method(
    ArcGISTiledElevationTerrainProvider,
    "fromUrl",
    () => pending.promise,
  );
  const provider = remoteProvider();
  const mesh = t.mock.method(
    I3SDataProvider,
    "fromUrl",
    async () => provider as unknown as I3SDataProvider,
  );
  const { viewer, primitives } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});
  t.after(() => layer.destroy());

  await layer.setEnabled(true);
  assert.equal(geoid.mock.callCount(), 1);
  await layer.setEnabled(false);
  pending.resolve({} as ArcGISTiledElevationTerrainProvider);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(mesh.mock.callCount(), 0);
  assert.equal(primitives.length, 0);
});

test("failed photogrammetry waits for a deliberate toggle instead of retrying on camera changes", async (t) => {
  installLocalLandmarkStubs(t);
  const load = t.mock.method(I3SDataProvider, "fromUrl", async () => {
    throw new Error("unavailable");
  });
  const { viewer, triggerMoveEnd } = fakeViewer();
  const reports: string[] = [];
  const layer = new LandmarksLayer(viewer, (message) => reports.push(message));
  t.after(() => layer.destroy());

  await layer.setEnabled(true);
  await eventually(() =>
    reports.some((message) => message.includes("unavailable")),
  );
  for (let i = 0; i < 5; i++) {
    triggerMoveEnd();
    await layer.setEnabled(true);
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(load.mock.callCount(), 1);
  assert.match(reports.at(-1) ?? "", /unavailable.*toggle to retry/);

  await layer.setEnabled(false);
  await layer.setEnabled(true);
  await eventually(() => load.mock.callCount() === 2);
});

test("landmarks off aborts pending chunks; stale completions cannot allocate after re-enabling", async (t) => {
  const { manifest, chunk } = buildingFixture();
  const firstChunk = deferred<Response>();
  const nextChunk = deferred<Response>();
  const signals: AbortSignal[] = [];
  installLocalLandmarkStubs(t, manifest, async (input, init) => {
    if (String(input).endsWith("/manifest.json")) return jsonResponse(manifest);
    assert.ok(init?.signal);
    signals.push(init.signal);
    // Deliberately ignore abort here to simulate a response already completing.
    return signals.length === 1 ? firstChunk.promise : nextChunk.promise;
  });
  const { viewer, primitives } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});
  t.after(() => layer.destroy());

  await layer.setEnabled(true, false);
  await eventually(() => signals.length === 1);
  await layer.setEnabled(false, false);
  assert.equal(signals[0]?.aborted, true);
  await layer.setEnabled(true, false);
  assert.equal(signals.length, 1, "the cancelled load still owns its slot");

  firstChunk.resolve(jsonResponse(chunk));
  await eventually(() => signals.length === 2);
  assert.equal(primitives.length, 0, "discard the cancelled result");
  assert.equal(signals[1]?.aborted, false);
  nextChunk.resolve(jsonResponse(chunk));
  await eventually(() => primitives.length === 1);
  await layer.setEnabled(false, false);
  assert.equal(primitives.length, 0);
});

test("destroy aborts pending chunks and prevents subsequent loads or allocations", async (t) => {
  const { manifest, chunk } = buildingFixture();
  const pendingChunk = deferred<Response>();
  let signal: AbortSignal | null | undefined;
  let requests = 0;
  installLocalLandmarkStubs(t, manifest, async (input, init) => {
    requests++;
    if (String(input).endsWith("/manifest.json")) return jsonResponse(manifest);
    signal = init?.signal;
    return pendingChunk.promise;
  });
  const { viewer, primitives, triggerMoveEnd } = fakeViewer();
  const layer = new LandmarksLayer(viewer, () => {});

  await layer.setEnabled(true, false);
  await eventually(() => signal != null);
  layer.destroy();
  assert.equal(signal?.aborted, true);
  pendingChunk.resolve(jsonResponse(chunk));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await layer.setEnabled(true, false);
  triggerMoveEnd();
  assert.equal(requests, 2);
  assert.equal(primitives.length, 0);
});

test("municipal fallback stays until mesh geometry is selected for rendering and returns when it is absent", async (t) => {
  const { manifest, chunk } = buildingFixture();
  // A small reference building inside the pilot mesh bounds exercises fallback.
  const building = chunk.buildings[0];
  assert.ok(building);
  building[4] = [
    [
      [
        [-104.9898, 39.7396],
        [-104.9895, 39.7396],
        [-104.9895, 39.7398],
        [-104.9898, 39.7398],
        [-104.9898, 39.7396],
      ],
    ],
  ];
  installLocalLandmarkStubs(t, manifest, async (input) =>
    jsonResponse(String(input).endsWith("/manifest.json") ? manifest : chunk),
  );
  const provider = remoteProvider();
  t.mock.method(
    I3SDataProvider,
    "fromUrl",
    async () => provider as unknown as I3SDataProvider,
  );
  const { viewer, primitives, renderFrame } = fakeViewer();
  const reports: string[] = [];
  const layer = new LandmarksLayer(viewer, (message) => reports.push(message));
  t.after(() => layer.destroy());

  await layer.setEnabled(true);
  await eventually(() => primitives.length === 3);
  const buildings = primitives.filter((value) => value !== provider) as {
    show: boolean;
  }[];
  assert.ok(buildings.every((primitive) => primitive.show));
  assert.match(reports.at(-1) ?? "", /photogrammetry loading/);
  const tileset = provider.layers[0]?.tileset;
  assert.ok(tileset);
  renderFrame(() =>
    tileset.tileVisible.raiseEvent({ content: { trianglesLength: 0 } }),
  );
  assert.ok(
    buildings.every((primitive) => primitive.show),
    "empty tile is not geometry",
  );

  renderFrame(() => {
    tileset.tileVisible.raiseEvent({ content: { trianglesLength: 12 } });
    assert.ok(
      buildings.every((primitive) => primitive.show),
      "do not mutate primitives during traversal",
    );
  });
  assert.equal(buildings.filter((primitive) => primitive.show).length, 1);
  assert.doesNotMatch(reports.at(-1) ?? "", /photogrammetry loading/);
  renderFrame();
  assert.ok(buildings.every((primitive) => primitive.show));

  tileset.tileFailed.raiseEvent({ message: "unavailable" });
  renderFrame();
  assert.equal(provider.destroyCount, 1);
  assert.ok(buildings.every((primitive) => primitive.show));
  assert.match(reports.at(-1) ?? "", /unavailable.*toggle to retry/);
  assert.equal(tileset.tileVisible.numberOfListeners, 0);
  assert.equal(tileset.tileFailed.numberOfListeners, 0);
  assert.equal(viewer.scene.preRender.numberOfListeners, 0);
  assert.equal(viewer.scene.postRender.numberOfListeners, 0);
});

test("oblique landmark bounds include visible elevated terrain beyond the ellipsoid rectangle", async (t) => {
  installLocalLandmarkStubs(t);
  const provider = remoteProvider();
  const load = t.mock.method(
    I3SDataProvider,
    "fromUrl",
    async () => provider as unknown as I3SDataProvider,
  );
  const { viewer, primitives } = fakeViewer();
  // At a close oblique view the same rays meet the zero-height ellipsoid
  // beyond the elevated Civic Center ground actually visible on screen.
  t.mock.method(viewer.camera, "computeViewRectangle", () =>
    Rectangle.fromDegrees(-104.98, 39.75, -104.96, 39.77),
  );
  t.mock.method(viewer.camera, "getPickRay", () => new Ray());
  t.mock.method(viewer.scene.globe, "pick", () =>
    Cartesian3.fromDegrees(-104.9896, 39.7397, 1580),
  );
  const layer = new LandmarksLayer(viewer, () => {});
  t.after(() => layer.destroy());
  await layer.setEnabled(true);
  await eventually(() => primitives.includes(provider));
  assert.equal(load.mock.callCount(), 1);
});
