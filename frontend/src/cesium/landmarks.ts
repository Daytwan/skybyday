import {
  ArcGISTiledElevationTerrainProvider,
  Cartesian2,
  Cartesian3,
  Cartographic,
  type Cesium3DTile,
  Math as CesiumMath,
  Color,
  ColorGeometryInstanceAttribute,
  Credit,
  DistanceDisplayCondition,
  GeoJsonDataSource,
  GeometryInstance,
  HeightReference,
  I3SDataProvider,
  LabelGraphics,
  LabelStyle,
  PerInstanceColorAppearance,
  PolygonGeometry,
  PolygonHierarchy,
  Primitive,
  ShadowMode,
  type Viewer,
} from "cesium";
import { navd88ToNad83Ellipsoid } from "./elevation.ts";
import {
  type BuildingManifest,
  type BuildingPolygon,
  type BuildingRecord,
  parseBuildingChunk,
  parseBuildingManifest,
  representativeCoordinate,
  type ViewBounds,
  visibleBuildingChunks,
} from "./landmark-data.ts";

const BUILDING_ROOT = "/landmarks/denver-buildings";
const MCNICHOLS_SCENE =
  "https://tiles.arcgis.com/tiles/zdB7qR0BtYrg0Xpl/arcgis/rest/services/McNichols_Building/SceneServer/layers/0";
const EGM2008_GEOID =
  "https://tiles.arcgis.com/tiles/GVgbJbqm8hXASVYi/arcgis/rest/services/EGM2008/ImageServer";
const MCNICHOLS_BOUNDS = {
  west: -104.9899674,
  south: 39.7395362,
  east: -104.9892272,
  north: 39.7399331,
} as const;
const BUILDING_LOAD_HEIGHT_M = 35_000;
const MESH_LOAD_HEIGHT_M = 12_000;
const MAX_VISIBLE_CHUNKS = 16;
const MAX_RETAINED_CHUNKS = 24;
const MAX_CONCURRENT_CHUNKS = 4;

interface LoadedChunk {
  primitive: Primitive;
  meshFallback?: Primitive;
  count: number;
  touched: number;
}

async function boundedJson(
  url: string,
  maxBytes: number,
  signal?: AbortSignal,
): Promise<unknown> {
  const timeout = AbortSignal.timeout(10_000);
  const response = await fetch(url, {
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    headers: { Accept: "application/json" },
  });
  signal?.throwIfAborted();
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const length = Number(response.headers.get("content-length"));
  if (Number.isFinite(length) && length > maxBytes)
    throw new Error("Response exceeds size limit");
  const text = await response.text();
  signal?.throwIfAborted();
  if (new TextEncoder().encode(text).length > maxBytes)
    throw new Error("Response exceeds size limit");
  return JSON.parse(text);
}

function buildingColor(type: string): Color {
  const normalized = type.toLowerCase();
  if (normalized.includes("public")) return Color.fromCssColorString("#d8ccb3");
  if (normalized.includes("commercial"))
    return Color.fromCssColorString("#c7d4d1");
  if (normalized.includes("industrial"))
    return Color.fromCssColorString("#aebcb9");
  if (normalized.includes("parking"))
    return Color.fromCssColorString("#b7b4aa");
  if (normalized.includes("medical"))
    return Color.fromCssColorString("#d6c1bb");
  return Color.fromCssColorString("#c5cbc4");
}

function hierarchy(polygon: BuildingPolygon, height: number): PolygonHierarchy {
  const ringPositions = (ring: BuildingPolygon[number]) =>
    ring
      .slice(0, -1)
      .map(([longitude, latitude]) =>
        Cartesian3.fromDegrees(longitude, latitude, height),
      );
  return new PolygonHierarchy(
    ringPositions(polygon[0] ?? []),
    polygon.slice(1).map((ring) => new PolygonHierarchy(ringPositions(ring))),
  );
}

function instancesForBuilding(building: BuildingRecord): GeometryInstance[] {
  const [longitude, latitude] = representativeCoordinate(building);
  const baseHeight = navd88ToNad83Ellipsoid(
    longitude,
    latitude,
    building[2],
    "geoid12a",
  );
  if (baseHeight === undefined) return [];
  const color = ColorGeometryInstanceAttribute.fromColor(
    buildingColor(building[3]),
  );
  return building[4].map(
    (polygon, index) =>
      new GeometryInstance({
        id: `denver-building-${building[0]}-${index}`,
        geometry: new PolygonGeometry({
          polygonHierarchy: hierarchy(polygon, baseHeight),
          height: baseHeight + building[1],
          extrudedHeight: baseHeight,
          closeTop: true,
          closeBottom: false,
          vertexFormat: PerInstanceColorAppearance.VERTEX_FORMAT,
        }),
        attributes: { color },
      }),
  );
}

function primitiveForBuildings(buildings: BuildingRecord[]): Primitive {
  return new Primitive({
    geometryInstances: buildings.flatMap(instancesForBuilding),
    appearance: new PerInstanceColorAppearance({
      closed: true,
      translucent: false,
      flat: false,
    }),
    asynchronous: true,
    allowPicking: false,
    compressVertices: true,
    releaseGeometryInstances: true,
    shadows: ShadowMode.ENABLED,
  });
}

function insideMcNichols(building: BuildingRecord): boolean {
  const [longitude, latitude] = representativeCoordinate(building);
  return (
    longitude >= MCNICHOLS_BOUNDS.west &&
    longitude <= MCNICHOLS_BOUNDS.east &&
    latitude >= MCNICHOLS_BOUNDS.south &&
    latitude <= MCNICHOLS_BOUNDS.north
  );
}

function intersects(left: ViewBounds, right: ViewBounds): boolean {
  return !(
    left.east < right.west ||
    left.west > right.east ||
    left.north < right.south ||
    left.south > right.north
  );
}

/**
 * Measured municipal roofprint volumes plus one bounded photogrammetry pilot.
 * The municipal scalar roof heights are real 2022 attributes, not roof planes.
 */
export class LandmarksLayer {
  private peaks?: GeoJsonDataSource;
  private peaksLoading?: Promise<void>;
  private manifest?: BuildingManifest;
  private manifestLoading?: Promise<void>;
  private readonly chunks = new Map<string, LoadedChunk>();
  private readonly queued = new Set<string>();
  private readonly loadingChunks = new Map<string, AbortController>();
  private wantedChunks = new Set<string>();
  private touchCounter = 0;
  private mesh?: I3SDataProvider;
  private meshLoading?: Promise<void>;
  private meshFailed = false;
  private meshContentVisible = false;
  private readonly meshListeners: (() => void)[] = [];
  private meshWanted = false;
  private allowRemote = true;
  private enabled = false;
  private destroyed = false;
  private readonly viewer: Viewer;
  private readonly report: (text: string) => void;
  private readonly removeMoveListener: () => void;

  constructor(viewer: Viewer, report: (text: string) => void) {
    this.viewer = viewer;
    this.report = report;
    this.removeMoveListener = this.viewer.camera.moveEnd.addEventListener(
      () => {
        void this.updateView();
      },
    );
  }

  async setEnabled(enabled: boolean, allowRemote = true): Promise<void> {
    if (this.destroyed) return;
    if (enabled && allowRemote && (!this.enabled || !this.allowRemote))
      this.meshFailed = false;
    this.enabled = enabled;
    this.allowRemote = allowRemote;
    if (this.peaks) this.peaks.show = enabled;
    if (!enabled || !allowRemote) this.releaseMesh();
    for (const [key, chunk] of this.chunks) this.setChunkVisibility(key, chunk);
    if (this.mesh) this.mesh.show = enabled && allowRemote && this.meshWanted;
    if (!enabled) {
      this.meshWanted = false;
      this.releaseBuildings();
      this.report("Denver measured buildings paused");
      this.viewer.scene.requestRender();
      return;
    }
    this.peaksLoading ??= this.loadPeaks().catch(() => {
      this.peaksLoading = undefined;
      if (this.enabled && !this.destroyed)
        this.report("Front Range peak labels unavailable · toggle to retry");
    });
    this.manifestLoading ??= this.loadManifest().catch(() => {
      this.manifestLoading = undefined;
      if (this.enabled && !this.destroyed)
        this.report("Denver measured buildings unavailable · toggle to retry");
    });
    await Promise.allSettled([this.peaksLoading, this.manifestLoading]);
    await this.updateView();
  }

  private async loadPeaks(): Promise<void> {
    const source = await GeoJsonDataSource.load("/landmarks/denver.geojson", {
      clampToGround: true,
    });
    if (this.destroyed) return;
    const now = this.viewer.clock.currentTime;
    for (const entity of source.entities.values) {
      if (entity.polygon) {
        entity.show = false;
        continue;
      }
      const name = String(entity.properties?.name?.getValue(now) ?? "");
      entity.name = `${name} · OpenStreetMap named peak reference`;
      entity.billboard = undefined;
      entity.label = new LabelGraphics({
        text: `△ ${name}`,
        font: "12px sans-serif",
        fillColor: Color.fromCssColorString("#e7d7b5"),
        style: LabelStyle.FILL_AND_OUTLINE,
        outlineColor: Color.fromCssColorString("#17272d"),
        outlineWidth: 3,
        pixelOffset: new Cartesian2(0, -8),
        heightReference: HeightReference.CLAMP_TO_GROUND,
        distanceDisplayCondition: new DistanceDisplayCondition(0, 90_000),
      });
    }
    this.peaks = source;
    source.show = this.enabled;
    await this.viewer.dataSources.add(source);
    this.viewer.scene.requestRender();
  }

  private async loadManifest(): Promise<void> {
    this.manifest = parseBuildingManifest(
      await boundedJson(`${BUILDING_ROOT}/manifest.json`, 256_000),
    );
    if (this.destroyed) return;
    this.viewer.cesiumWidget.creditDisplay.addStaticCredit(
      new Credit(
        '<a href="/map-attribution.html" target="_blank">City and County of Denver · 2022 building roofprints</a>',
        true,
      ),
    );
  }

  private view():
    | {
        bounds: ViewBounds;
        focus: [longitude: number, latitude: number];
        height: number;
      }
    | undefined {
    const cartographic = Cartographic.fromCartesian(
      this.viewer.camera.positionWC,
    );
    if (!cartographic) return undefined;
    const rectangle = this.viewer.camera.computeViewRectangle(
      this.viewer.scene.globe.ellipsoid,
    );
    if (!rectangle || rectangle.east < rectangle.west) return undefined;
    const canvas = this.viewer.scene.canvas;
    const bounds = {
      west: CesiumMath.toDegrees(rectangle.west),
      south: CesiumMath.toDegrees(rectangle.south),
      east: CesiumMath.toDegrees(rectangle.east),
      north: CesiumMath.toDegrees(rectangle.north),
    };
    let focusCartographic = cartographic;
    // computeViewRectangle intersects the ellipsoid, which can lie far beyond
    // elevated ground in close oblique views. Include actual terrain hits at
    // nine viewport points; keep the existing ellipsoid bounds at the horizon.
    for (const x of [0, 0.5, 1]) {
      for (const y of [0, 0.5, 1]) {
        const ray = this.viewer.camera.getPickRay(
          new Cartesian2(canvas.clientWidth * x, canvas.clientHeight * y),
        );
        const picked = ray
          ? this.viewer.scene.globe.pick(ray, this.viewer.scene)
          : undefined;
        if (!picked) continue;
        const point = Cartographic.fromCartesian(picked);
        const longitude = CesiumMath.toDegrees(point.longitude);
        const latitude = CesiumMath.toDegrees(point.latitude);
        bounds.west = Math.min(bounds.west, longitude);
        bounds.east = Math.max(bounds.east, longitude);
        bounds.south = Math.min(bounds.south, latitude);
        bounds.north = Math.max(bounds.north, latitude);
        if (x === 0.5 && y === 0.5) focusCartographic = point;
      }
    }
    return {
      bounds,
      focus: [
        CesiumMath.toDegrees(focusCartographic.longitude),
        CesiumMath.toDegrees(focusCartographic.latitude),
      ],
      height: cartographic.height,
    };
  }

  private async updateView(): Promise<void> {
    if (!this.enabled || this.destroyed || !this.manifest) return;
    const view = this.view();
    if (!view || view.bounds.east < view.bounds.west) return;
    const keys =
      view.height <= BUILDING_LOAD_HEIGHT_M
        ? visibleBuildingChunks(
            this.manifest,
            view.bounds,
            view.focus,
            MAX_VISIBLE_CHUNKS,
          )
        : [];
    this.wantedChunks = new Set(keys);
    for (const [key, chunk] of this.chunks) {
      this.setChunkVisibility(key, chunk);
      if (this.wantedChunks.has(key)) chunk.touched = ++this.touchCounter;
    }
    for (const key of keys) {
      if (
        !this.chunks.has(key) &&
        !this.queued.has(key) &&
        !this.loadingChunks.has(key)
      )
        this.queued.add(key);
    }
    this.meshWanted =
      view.height <= MESH_LOAD_HEIGHT_M &&
      intersects(view.bounds, MCNICHOLS_BOUNDS);
    if (this.mesh)
      this.mesh.show = this.enabled && this.allowRemote && this.meshWanted;
    if (
      this.meshWanted &&
      this.allowRemote &&
      !this.mesh &&
      !this.meshLoading &&
      !this.meshFailed
    ) {
      this.meshLoading = this.loadMcNichols()
        .catch(() => {
          if (this.enabled && this.allowRemote && !this.destroyed) {
            this.meshFailed = true;
            this.updateStatus();
          }
        })
        .finally(() => {
          this.meshLoading = undefined;
        });
    }
    for (const [key, chunk] of this.chunks) this.setChunkVisibility(key, chunk);
    this.pump();
    this.updateStatus();
  }

  private pump(): void {
    while (
      this.enabled &&
      !this.destroyed &&
      this.loadingChunks.size < MAX_CONCURRENT_CHUNKS &&
      this.queued.size > 0
    ) {
      const key = this.queued.values().next().value as string;
      this.queued.delete(key);
      if (
        !this.wantedChunks.has(key) ||
        this.chunks.has(key) ||
        this.loadingChunks.has(key)
      )
        continue;
      const controller = new AbortController();
      this.loadingChunks.set(key, controller);
      void this.loadChunk(key, controller.signal)
        .catch(() => {
          if (this.enabled && !this.destroyed && !controller.signal.aborted)
            this.report(
              "Some Denver building detail is unavailable · move camera to retry",
            );
        })
        .finally(() => {
          this.loadingChunks.delete(key);
          // A quick off/on must wait for cancelled requests to release their
          // slots before reloading, preserving the global concurrency bound.
          if (
            controller.signal.aborted &&
            this.enabled &&
            !this.destroyed &&
            this.wantedChunks.has(key)
          )
            this.queued.add(key);
          this.pump();
        });
    }
  }

  private async loadChunk(key: string, signal: AbortSignal): Promise<void> {
    const entry = this.manifest?.chunks[key];
    if (!entry) return;
    const data = await boundedJson(
      `${BUILDING_ROOT}/${key}.json`,
      1_000_000,
      signal,
    );
    if (
      this.destroyed ||
      !this.enabled ||
      signal.aborted ||
      this.chunks.has(key)
    )
      return;
    const buildings = parseBuildingChunk(data);
    if (buildings.length !== entry.count)
      throw new Error("Building chunk count does not match manifest");
    const measured = buildings.filter((building) => !insideMcNichols(building));
    const meshFallbackBuildings = buildings.filter(insideMcNichols);
    const primitive = primitiveForBuildings(measured);
    this.viewer.scene.primitives.add(primitive);
    const meshFallback =
      meshFallbackBuildings.length > 0
        ? primitiveForBuildings(meshFallbackBuildings)
        : undefined;
    if (meshFallback) this.viewer.scene.primitives.add(meshFallback);
    this.chunks.set(key, {
      primitive,
      meshFallback,
      count: buildings.length,
      touched: ++this.touchCounter,
    });
    this.setChunkVisibility(key, this.chunks.get(key) as LoadedChunk);
    this.trimChunks();
    this.updateStatus();
    this.viewer.scene.requestRender();
  }

  private trimChunks(): void {
    if (this.chunks.size <= MAX_RETAINED_CHUNKS) return;
    const removable = [...this.chunks.entries()]
      .filter(([key]) => !this.wantedChunks.has(key))
      .sort((left, right) => left[1].touched - right[1].touched);
    while (this.chunks.size > MAX_RETAINED_CHUNKS && removable.length > 0) {
      const [key, chunk] = removable.shift() as [string, LoadedChunk];
      this.viewer.scene.primitives.remove(chunk.primitive);
      if (chunk.meshFallback)
        this.viewer.scene.primitives.remove(chunk.meshFallback);
      this.chunks.delete(key);
    }
  }

  private async loadMcNichols(): Promise<void> {
    const geoid =
      await ArcGISTiledElevationTerrainProvider.fromUrl(EGM2008_GEOID);
    if (this.destroyed || !this.enabled || !this.allowRemote) return;
    const provider = await I3SDataProvider.fromUrl(MCNICHOLS_SCENE, {
      name: "McNichols Building · Denver photogrammetry",
      geoidTiledTerrainProvider: geoid,
      cesium3dTilesetOptions: {
        maximumScreenSpaceError: 8,
        cacheBytes: 64 * 1024 * 1024,
        maximumCacheOverflowBytes: 64 * 1024 * 1024,
        preloadWhenHidden: false,
        shadows: ShadowMode.ENABLED,
      },
    });
    if (this.destroyed || !this.enabled || !this.allowRemote) {
      provider.destroy();
      return;
    }
    provider.show = this.enabled && this.meshWanted;
    this.viewer.scene.primitives.add(provider);
    this.mesh = provider;
    this.watchMeshContent(provider);
    for (const [key, chunk] of this.chunks) this.setChunkVisibility(key, chunk);
    this.viewer.cesiumWidget.creditDisplay.addStaticCredit(
      new Credit(
        '<a href="https://www.arcgis.com/home/item.html?id=c2a3283542294d0b8f93c08230668a49" target="_blank">McNichols photogrammetry · City and County of Denver</a>',
        true,
      ),
    );
    this.viewer.cesiumWidget.creditDisplay.addStaticCredit(
      new Credit(
        "EGM2008 geoid · National Geospatial-Intelligence Agency",
        true,
      ),
    );
    this.updateStatus();
    this.viewer.scene.requestRender();
  }

  private watchMeshContent(provider: I3SDataProvider): void {
    let visibleThisFrame = false;
    let failed = false;
    for (const layer of provider.layers) {
      if (!layer.tileset) continue;
      this.meshListeners.push(
        layer.tileset.tileVisible.addEventListener((tile: Cesium3DTile) => {
          if (tile.content.trianglesLength > 0) visibleThisFrame = true;
        }),
        layer.tileset.tileFailed.addEventListener(() => {
          failed = true;
          this.viewer.scene.requestRender();
        }),
      );
    }
    this.meshListeners.push(
      this.viewer.scene.preRender.addEventListener(() => {
        visibleThisFrame = false;
      }),
      this.viewer.scene.postRender.addEventListener(() => {
        // tileVisible fires during traversal. Change fallback primitives only
        // after rendering, and only when selected geometry or failure changes.
        const visible = provider.show && visibleThisFrame;
        if (!failed && visible === this.meshContentVisible) return;
        if (failed) {
          this.meshFailed = true;
          this.releaseMesh();
        } else {
          this.meshContentVisible = visible;
        }
        for (const [key, chunk] of this.chunks)
          this.setChunkVisibility(key, chunk);
        this.updateStatus();
        this.viewer.scene.requestRender();
      }),
    );
  }

  private updateStatus(): void {
    if (this.destroyed || !this.enabled) return;
    const visible = [...this.chunks.entries()].filter(
      ([key, chunk]) => this.wantedChunks.has(key) && chunk.primitive.show,
    );
    const count = visible.reduce((sum, [, chunk]) => sum + chunk.count, 0);
    const detail =
      this.mesh?.show && this.meshContentVisible
        ? " · McNichols measured photogrammetry"
        : this.meshWanted && this.allowRemote
          ? this.meshFailed
            ? " · McNichols photogrammetry unavailable · measured city volumes remain · toggle to retry"
            : " · McNichols photogrammetry loading"
          : "";
    this.report(
      count > 0
        ? `City of Denver 2022 roofprints · ${count.toLocaleString()} visible${detail}`
        : `City of Denver 2022 roofprints · zoom closer for measured detail${detail}`,
    );
  }

  private setChunkVisibility(key: string, chunk: LoadedChunk): void {
    const visible = this.enabled && this.wantedChunks.has(key);
    chunk.primitive.show = visible;
    if (chunk.meshFallback)
      chunk.meshFallback.show =
        visible && !(this.mesh?.show && this.meshContentVisible);
  }

  private releaseBuildings(): void {
    this.queued.clear();
    this.wantedChunks.clear();
    for (const controller of this.loadingChunks.values()) controller.abort();
    for (const chunk of this.chunks.values()) {
      this.viewer.scene.primitives.remove(chunk.primitive);
      if (chunk.meshFallback)
        this.viewer.scene.primitives.remove(chunk.meshFallback);
    }
    this.chunks.clear();
  }

  private releaseMesh(): void {
    for (const remove of this.meshListeners) remove();
    this.meshListeners.length = 0;
    this.meshContentVisible = false;
    if (!this.mesh) return;
    this.viewer.scene.primitives.remove(this.mesh);
    this.mesh = undefined;
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.removeMoveListener();
    this.releaseBuildings();
    this.releaseMesh();
    if (this.peaks) this.viewer.dataSources.remove(this.peaks, true);
  }
}
