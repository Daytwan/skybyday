import assert from "node:assert/strict";
import test from "node:test";
import {
  Cartographic,
  EllipsoidTerrainProvider,
  Rectangle,
  Request,
  Resource,
  TileMapServiceImageryProvider,
  TileProviderError,
  UrlTemplateImageryProvider,
  type Viewer,
  WebMapTileServiceImageryProvider,
} from "cesium";
import { MapLayers } from "./maps.ts";
import { build3depExportUrl, OpenTerrainProvider } from "./terrain.ts";

function fakeViewer() {
  const layers: { imageryProvider: UrlTemplateImageryProvider }[] = [];
  const removed: { layer: object; destroy: boolean }[] = [];
  const view = {
    imageryLayers: {
      add(layer: { imageryProvider: UrlTemplateImageryProvider }) {
        layers.push(layer);
        return layer;
      },
      addImageryProvider(
        provider: UrlTemplateImageryProvider,
        index = layers.length,
      ) {
        const layer = { imageryProvider: provider };
        layers.splice(index, 0, layer);
        return layer;
      },
      remove(
        layer: { imageryProvider: UrlTemplateImageryProvider },
        destroy = true,
      ) {
        const index = layers.indexOf(layer);
        if (index < 0) return false;
        layers.splice(index, 1);
        removed.push({ layer, destroy });
        return true;
      },
    },
    terrainProvider: new EllipsoidTerrainProvider(),
    scene: { requestRender() {} },
  };
  return { view, viewer: view as unknown as Viewer, layers, removed };
}

test("map failures retain local imagery and flat terrain without retrying until asked", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  const { viewer, layers, view } = fakeViewer();
  let status = "";
  const maps = new MapLayers(viewer, (text) => {
    status = text;
  });
  const preferences = {
    imagery: "aerial" as const,
    terrain: true,
    airport: false,
    places: true,
    landmarks: true,
  };
  await maps.apply(preferences);
  assert.equal(layers.length, 3);
  const terrain = view.terrainProvider;
  const detail = layers[2]?.imageryProvider;
  assert.equal(detail?.maximumLevel, 18);
  assert.equal(detail?.minimumLevel, 10);
  // Cesium 1.146 stores this constructor option without a public getter.
  assert.equal(
    (layers[2] as unknown as { _minimumTerrainLevel: number })
      ._minimumTerrainLevel,
    14,
  );
  assert.deepEqual(
    detail?.rectangle,
    Rectangle.fromDegrees(-105.8, 39.4, -104.4, 40.1),
  );
  assert.equal(layers[1]?.imageryProvider.maximumLevel, 16);
  detail?.errorEvent.raiseEvent(new Error("HTTP 503"));
  await Promise.resolve();
  assert.equal(layers.length, 3);
  assert.match(status, /Some NAIP tiles unavailable/);
  detail?.errorEvent.raiseEvent(new Error("HTTP 503"));
  await Promise.resolve();
  assert.equal(layers.length, 3);
  detail?.errorEvent.raiseEvent(new Error("HTTP 503"));
  await Promise.resolve();
  assert.equal(layers.length, 2);
  assert.match(status, /NAIP detail unavailable/);
  layers[1]?.imageryProvider.errorEvent.raiseEvent(new Error("HTTP 429"));
  terrain.errorEvent.raiseEvent(new Error("Offline"));
  await Promise.resolve();
  assert.equal(layers.length, 1);
  assert.ok(view.terrainProvider instanceof EllipsoidTerrainProvider);
  assert.match(status, /aerial unavailable.*Terrain unavailable/);
  await maps.apply(preferences);
  assert.equal(layers.length, 1);
  await maps.apply(preferences, true);
  assert.equal(layers.length, 3);
  assert.notEqual(layers[2]?.imageryProvider, detail);
  layers[2]?.imageryProvider.errorEvent.raiseEvent(new Error("HTTP 503"));
  await Promise.resolve();
  assert.equal(layers.length, 3);
  assert.ok(view.terrainProvider instanceof OpenTerrainProvider);
  maps.destroy();
});

test("a late initial map load cannot override the latest offline selection", async (t) => {
  let resolve!: (provider: UrlTemplateImageryProvider) => void;
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    () =>
      new Promise<UrlTemplateImageryProvider>((done) => {
        resolve = done;
      }),
  );
  const { viewer, layers, view } = fakeViewer();
  const maps = new MapLayers(viewer, () => {});
  const first = maps.apply({
    imagery: "aerial",
    terrain: true,
    airport: false,
    places: true,
    landmarks: true,
  });
  const second = maps.apply({
    imagery: "offline",
    terrain: true,
    airport: false,
    places: true,
    landmarks: true,
  });
  resolve(new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }));
  await Promise.all([first, second]);
  assert.equal(layers.length, 1);
  assert.ok(view.terrainProvider instanceof EllipsoidTerrainProvider);
  maps.destroy();
});

test("NASA Earth relief supplies bounded global cartography without US-only detail", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  const { viewer, layers } = fakeViewer();
  const maps = new MapLayers(viewer, () => {});
  await maps.apply({
    imagery: "earth",
    terrain: true,
    airport: false,
    places: true,
    landmarks: true,
  });
  assert.equal(layers.length, 2);
  assert.ok(
    layers[1]?.imageryProvider instanceof WebMapTileServiceImageryProvider,
  );
  const earth = layers[1]?.imageryProvider as WebMapTileServiceImageryProvider;
  assert.equal(earth.maximumLevel, 8);
  assert.match(earth.url, /gibs\.earthdata\.nasa\.gov/);
  maps.destroy();
});

test("offline selection and destroy remove owned imagery while preserving other layers", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  const { viewer, layers, view, removed } = fakeViewer();
  const maps = new MapLayers(viewer, () => {});
  const preferences = {
    imagery: "aerial" as const,
    terrain: false,
    airport: false,
    places: false,
    landmarks: false,
  };
  await maps.apply(preferences);
  const unrelated = view.imageryLayers.addImageryProvider(
    new UrlTemplateImageryProvider({ url: "/other/{z}/{x}/{y}.png" }),
  );
  const background = layers[0];
  await maps.apply({ ...preferences, imagery: "offline" });
  assert.equal(layers.length, 2);
  assert.equal(layers[0], background);
  assert.equal(layers[1], unrelated);
  await maps.apply(preferences);
  assert.equal(layers.length, 4);
  maps.destroy();
  assert.equal(layers.length, 1);
  assert.equal(layers[0], unrelated);
  assert.ok(removed.every((entry) => entry.destroy));
  maps.destroy();
  assert.equal(layers.length, 1);
  assert.equal(layers[0], unrelated);
});

test("old imagery errors cannot remove or report failure for successor layers", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  const { viewer, layers } = fakeViewer();
  let status = "";
  const maps = new MapLayers(viewer, (text) => {
    status = text;
  });
  t.after(() => maps.destroy());
  const preferences = {
    imagery: "aerial" as const,
    terrain: false,
    airport: false,
    places: false,
    landmarks: false,
  };
  await maps.apply(preferences);
  const general = layers[1]?.imageryProvider;
  const detail = layers[2]?.imageryProvider;
  const switching = maps.apply({ ...preferences, imagery: "earth" });
  general?.errorEvent.raiseEvent(new Error("late general failure"));
  await switching;
  await Promise.resolve();
  assert.equal(layers.length, 2);
  const earth = layers[1];
  assert.ok(earth?.imageryProvider instanceof WebMapTileServiceImageryProvider);
  const successorStatus = status;
  detail?.errorEvent.raiseEvent(new Error("late detail failure"));
  await Promise.resolve();
  assert.equal(layers[1], earth);
  assert.equal(status, successorStatus);
  const returning = maps.apply(preferences);
  earth.imageryProvider.errorEvent.raiseEvent(new Error("late NASA failure"));
  await returning;
  await Promise.resolve();
  assert.equal(layers.length, 3);
  assert.match(status, /USGS aerial/);
  const retryDetail = layers[2]?.imageryProvider;
  const leavingDetail = maps.apply({ ...preferences, imagery: "earth" });
  for (let i = 0; i < 3; i++)
    retryDetail?.errorEvent.raiseEvent(new Error("late queued detail failure"));
  await leavingDetail;
  await Promise.resolve();
  assert.equal(layers.length, 2);
  assert.ok(
    layers[1]?.imageryProvider instanceof WebMapTileServiceImageryProvider,
  );
  assert.doesNotMatch(status, /unavailable/);
});

test("NAIP throttling stops detail on its first failure until explicit retry", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  const { viewer, layers } = fakeViewer();
  const maps = new MapLayers(viewer, () => {});
  t.after(() => maps.destroy());
  const preferences = {
    imagery: "aerial" as const,
    terrain: false,
    airport: false,
    places: false,
    landmarks: false,
  };
  await maps.apply(preferences);
  const background = layers[0];
  const general = layers[1];
  const detail = layers[2]?.imageryProvider;
  assert.ok(detail);
  const error = new TileProviderError(
    detail,
    "throttled",
    0,
    0,
    10,
    0,
    Object.assign(new Error("HTTP 429"), { statusCode: 429 }),
  );
  detail.errorEvent.raiseEvent(error);
  assert.equal(error.retry, false);
  await Promise.resolve();
  assert.equal(layers.length, 2);
  assert.equal(layers[0], background);
  assert.equal(layers[1], general);
  await maps.apply(preferences);
  assert.equal(layers.length, 2);
  await maps.apply(preferences, true);
  assert.equal(layers.length, 3);
  assert.notEqual(layers[2]?.imageryProvider, detail);
});

test("general and NASA failures retain background and require explicit retry", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  for (const imagery of ["aerial", "earth"] as const) {
    await t.test(imagery, async () => {
      const { viewer, layers } = fakeViewer();
      let status = "";
      const maps = new MapLayers(viewer, (text) => {
        status = text;
      });
      try {
        const preferences = {
          imagery,
          terrain: false,
          airport: false,
          places: false,
          landmarks: false,
        };
        await maps.apply(preferences);
        const background = layers[0];
        const failed = layers[1];
        assert.ok(failed);
        const error = new TileProviderError(
          failed.imageryProvider,
          "HTTP 503",
          0,
          0,
          0,
        );
        failed.imageryProvider.errorEvent.raiseEvent(error);
        assert.equal(error.retry, false);
        await Promise.resolve();
        assert.equal(layers[0], background);
        assert.ok(!layers.includes(failed));
        assert.match(status, /unavailable.*offline background/);
        const retained = [...layers];
        await maps.apply(preferences);
        assert.deepEqual(layers, retained);
        await maps.apply(preferences, true);
        assert.equal(layers.length, imagery === "earth" ? 2 : 3);
        assert.notEqual(layers[1], failed);
      } finally {
        maps.destroy();
      }
    });
  }
});

test("destroy during local initialization cannot attach imagery afterwards", async (t) => {
  let resolve!: (provider: UrlTemplateImageryProvider) => void;
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    () =>
      new Promise<UrlTemplateImageryProvider>((done) => {
        resolve = done;
      }),
  );
  const { viewer, layers, view } = fakeViewer();
  const unrelated = view.imageryLayers.addImageryProvider(
    new UrlTemplateImageryProvider({ url: "/other/{z}/{x}/{y}.png" }),
  );
  let reports = 0;
  const maps = new MapLayers(viewer, () => {
    reports++;
  });
  const pending = maps.apply({
    imagery: "aerial",
    terrain: false,
    airport: false,
    places: false,
    landmarks: false,
  });
  maps.destroy();
  resolve(new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }));
  await pending;
  assert.equal(layers.length, 1);
  assert.equal(layers[0], unrelated);
  assert.equal(reports, 0);
});

test("terrain request limits, failure reporting, and shutdown do not require network access", async (t) => {
  const fetch = t.mock.method(Resource.prototype, "fetchImage", () =>
    Promise.reject(new Error("HTTP 503")),
  );
  const terrain = new OpenTerrainProvider();
  let errors = 0;
  terrain.errorEvent.addEventListener(() => {
    errors++;
  });
  assert.equal(terrain.requestTileGeometry(0, 0, 16), undefined);
  assert.equal(fetch.mock.callCount(), 0);
  const failed = terrain.requestTileGeometry(0, 0, 0);
  assert.ok(failed);
  await assert.rejects(failed, /503/);
  assert.equal(errors, 1);
  let reject!: (reason: Error) => void;
  fetch.mock.mockImplementation(
    () =>
      new Promise<HTMLImageElement | ImageBitmap>((_, fail) => {
        reject = fail;
      }),
  );
  let cancelled = false;
  const request = new Request({
    cancelFunction: () => {
      cancelled = true;
      reject(new Error("cancelled"));
    },
  });
  const pending = terrain.requestTileGeometry(0, 0, 0, request);
  assert.ok(pending);
  terrain.stop();
  await assert.rejects(pending, /cancelled/);
  assert.ok(cancelled);
  assert.equal(errors, 1);
  assert.equal(terrain.requestTileGeometry(0, 0, 0), undefined);
});

test("3DEP detail is bounded and a detail failure keeps world terrain available", async (t) => {
  const fetchDetail = t.mock.method(
    Resource.prototype,
    "fetchArrayBuffer",
    () => Promise.reject(new Error("HTTP 503")),
  );
  const fetchWorld = t.mock.method(Resource.prototype, "fetchImage", () =>
    Promise.reject(new Error("world request reached")),
  );
  let detailFailures = 0;
  let baseFailures = 0;
  const terrain = new OpenTerrainProvider(() => {
    detailFailures++;
  });
  terrain.errorEvent.addEventListener(() => {
    baseFailures++;
  });
  const den = terrain.tilingScheme.positionToTileXY(
    Cartographic.fromDegrees(-104.9903, 39.7392),
    16,
  );
  assert.ok(den);
  assert.equal(terrain.getTileDataAvailable(den.x, den.y, 16), true);
  const detail = terrain.requestTileGeometry(den.x, den.y, 16);
  assert.ok(detail);
  await assert.rejects(detail, /503/);
  assert.equal(detailFailures, 1);
  assert.equal(baseFailures, 0);
  assert.equal(terrain.getTileDataAvailable(den.x, den.y, 16), false);
  assert.equal(fetchDetail.mock.callCount(), 1);

  const world = terrain.requestTileGeometry(0, 0, 0);
  assert.ok(world);
  await assert.rejects(world, /world request reached/);
  assert.equal(fetchWorld.mock.callCount(), 1);
  assert.equal(baseFailures, 1);
  terrain.stop();
});

test("3DEP export requests 129 aligned float samples in Web Mercator", () => {
  const url = new URL(
    build3depExportUrl({ west: 0, south: 0, east: 64, north: 64 }),
  );
  assert.equal(url.searchParams.get("bbox"), "-0.25,-0.25,64.25,64.25");
  assert.equal(url.searchParams.get("size"), "129,129");
  assert.equal(url.searchParams.get("format"), "lerc");
  assert.equal(url.searchParams.get("pixelType"), "F32");
  assert.equal(url.searchParams.get("bboxSR"), "3857");
});

test("Cesium's reported 3DEP tile failure preserves world terrain; base failure still falls back", async (t) => {
  t.mock.method(
    TileMapServiceImageryProvider,
    "fromUrl",
    async () =>
      new UrlTemplateImageryProvider({ url: "/offline/{z}/{x}/{y}.png" }),
  );
  t.mock.method(Resource.prototype, "fetchArrayBuffer", async () => {
    throw new Error("3DEP unavailable");
  });
  const { viewer, view } = fakeViewer();
  let status = "";
  const maps = new MapLayers(viewer, (message) => {
    status = message;
  });
  t.after(() => maps.destroy());
  await maps.apply({
    imagery: "aerial",
    terrain: true,
    airport: false,
    places: false,
    landmarks: false,
  });
  const terrain = view.terrainProvider;
  assert.ok(terrain instanceof OpenTerrainProvider);
  const tile = terrain.tilingScheme.positionToTileXY(
    Cartographic.fromDegrees(-104.9903, 39.7392),
    16,
  );
  assert.ok(tile);
  const request = terrain.requestTileGeometry(tile.x, tile.y, 16);
  assert.ok(request);
  await assert.rejects(request, /3DEP unavailable/);
  // GlobeSurfaceTile reports a rejected terrain request through this same
  // event after the provider's own failure handler has run.
  const error = new TileProviderError(
    terrain,
    "Failed to obtain terrain tile",
    tile.x,
    tile.y,
    16,
  );
  terrain.errorEvent.raiseEvent(error);
  await Promise.resolve();
  assert.equal(error.retry, false);
  assert.equal(view.terrainProvider, terrain);
  assert.match(status, /world terrain · 3DEP detail unavailable/);
  assert.equal(terrain.getTileDataAvailable(tile.x, tile.y, 16), false);

  terrain.errorEvent.raiseEvent(
    new TileProviderError(terrain, "World terrain unavailable", 0, 0, 0),
  );
  await Promise.resolve();
  assert.ok(view.terrainProvider instanceof EllipsoidTerrainProvider);
  assert.match(status, /flat fallback/);
});
