import {
  Cartesian2,
  Cartesian3,
  Color,
  ColorMaterialProperty,
  ConstantPositionProperty,
  ConstantProperty,
  Credit,
  DistanceDisplayCondition,
  EllipsoidTerrainProvider,
  GeoJsonDataSource,
  HeightReference,
  ImageryLayer,
  LabelGraphics,
  LabelStyle,
  type PolygonHierarchy,
  Rectangle,
  TileMapServiceImageryProvider,
  TileProviderError,
  UrlTemplateImageryProvider,
  type Viewer,
  WebMapTileServiceImageryProvider,
} from "cesium";
import {
  TERRAIN_DETAIL_MAX_LEVEL,
  TERRAIN_DETAIL_RECTANGLE,
  TERRAIN_MAX_LEVEL,
} from "./elevation.ts";
import type { MapPreferences } from "./preferences.ts";
import { OpenTerrainProvider } from "./terrain.ts";

const AIRPORT_CREDIT = new Credit(
  '<a href="/map-attribution.html" target="_blank">FAA airport reference</a>',
  true,
);
const NASA_BLUE_MARBLE_URL =
  "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/default/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.jpeg";
const AIRPORT_COLORS: Record<string, string> = {
  runway: "#f2eee1",
  taxiway: "#e9cc80",
  apron: "#7db9bb",
  building: "#c2d3e0",
};

export class MapLayers {
  private readonly viewer: Viewer;
  private readonly report: (message: string) => void;
  private local?: Promise<void>;
  private background?: ImageryLayer;
  private aerial?: ImageryLayer;
  private detail?: ImageryLayer;
  private terrain?: OpenTerrainProvider;
  private airport?: GeoJsonDataSource;
  private airportLoading?: Promise<void>;
  private generation = 0;
  private destroyed = false;
  private activeImagery?: MapPreferences["imagery"];
  private activeTerrain?: boolean;
  private preferences: MapPreferences = {
    imagery: "offline",
    terrain: false,
    airport: false,
    places: false,
    landmarks: false,
  };
  private imageryStatus = "Loading base map";
  private detailStatus = "";
  private terrainStatus = "flat surface";
  private airportStatus = "";

  constructor(viewer: Viewer, report: (message: string) => void) {
    this.viewer = viewer;
    this.report = report;
  }

  private status(): void {
    if (!this.destroyed) {
      this.report(
        [
          this.imageryStatus,
          this.detailStatus,
          this.terrainStatus,
          this.airportStatus,
        ]
          .filter(Boolean)
          .join(" · "),
      );
      this.viewer.scene.requestRender();
    }
  }

  private async localMap(): Promise<void> {
    const provider = await TileMapServiceImageryProvider.fromUrl(
      "/cesium/Assets/Textures/NaturalEarthII",
      {
        credit: "Natural Earth II · public domain",
        maximumLevel: 2,
      },
    );
    if (this.destroyed) return;
    this.background = this.viewer.imageryLayers.addImageryProvider(provider, 0);
  }

  async apply(preferences: MapPreferences, retry = false): Promise<void> {
    const generation = ++this.generation;
    this.preferences = { ...preferences };
    this.local ??= this.localMap();
    await this.local;
    if (this.destroyed || generation !== this.generation) return;
    if (retry || this.activeImagery !== preferences.imagery) {
      this.setImagery(preferences.imagery);
      this.activeImagery = preferences.imagery;
    }
    const terrain = preferences.imagery !== "offline" && preferences.terrain;
    if (retry || this.activeTerrain !== terrain) {
      this.setTerrain(terrain);
      this.activeTerrain = terrain;
    }
    if (this.airport) this.airport.show = preferences.airport;
    if (preferences.airport) {
      this.airportStatus = "loading DEN overlay";
      this.airportLoading ??= this.loadAirport().catch(() => {
        this.airportLoading = undefined;
        this.airportStatus = this.preferences.airport
          ? "DEN overlay unavailable (Retry maps)"
          : "";
        this.status();
      });
      await this.airportLoading;
      if (this.destroyed || generation !== this.generation) return;
      if (this.airport) this.airportStatus = "DEN airport reference";
    } else this.airportStatus = "";
    this.status();
  }

  private setImagery(imagery: MapPreferences["imagery"]): void {
    if (this.detail) {
      this.viewer.imageryLayers.remove(this.detail, true);
      this.detail = undefined;
    }
    this.detailStatus = "";
    if (this.aerial) {
      this.viewer.imageryLayers.remove(this.aerial, true);
      this.aerial = undefined;
    }
    this.imageryStatus = "Offline world map";
    if (imagery === "aerial") {
      const provider = new UrlTemplateImageryProvider({
        url: "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
        // The service advertises cached imagery through 1:9028 (level 16).
        // Finer LOD entries in metadata do not establish finer imagery coverage.
        maximumLevel: 16,
        credit: new Credit(
          '<a href="https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer" target="_blank">USGS / USDA</a>',
          true,
        ),
        enablePickFeatures: false,
      });
      const layer = this.viewer.imageryLayers.addImageryProvider(provider);
      this.aerial = layer;
      this.imageryStatus = "USGS aerial · detailed US coverage";
      provider.errorEvent.addEventListener(() => {
        queueMicrotask(() => {
          if (this.destroyed || this.aerial !== layer) return;
          // End this attempt, including throttling. Only an explicit retry starts another.
          this.viewer.imageryLayers.remove(layer, true);
          this.aerial = undefined;
          this.imageryStatus =
            "General aerial unavailable · offline background (Retry map services)";
          this.status();
        });
      });
      // Finer public NAIP imagery is bounded to the Denver / Front Range scenes.
      // No bulk downloads or global dynamic-image requests. Cesium schedules
      // only visible tiles; the layer preserves the source service's 0.6 m
      // Denver-area detail without claiming a uniform acquisition year.
      const detailProvider = new UrlTemplateImageryProvider({
        url: "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage?bbox={westProjected}%2C{southProjected}%2C{eastProjected}%2C{northProjected}&bboxSR=3857&imageSR=3857&size=256%2C256&format=png32&interpolation=RSP_BilinearInterpolation&f=image",
        rectangle: Rectangle.fromDegrees(
          TERRAIN_DETAIL_RECTANGLE.west,
          TERRAIN_DETAIL_RECTANGLE.south,
          TERRAIN_DETAIL_RECTANGLE.east,
          TERRAIN_DETAIL_RECTANGLE.north,
        ),
        minimumLevel: 10,
        maximumLevel: 18,
        credit: new Credit(
          '<a href="/map-attribution.html" target="_blank">NAIP</a>',
          true,
        ),
        enablePickFeatures: false,
      });
      // The cached base already covers broad views. Dynamic NAIP exports are
      // reserved for close terrain tiles, where the additional detail is useful.
      const detail = new ImageryLayer(detailProvider, {
        minimumTerrainLevel: 14,
      });
      this.viewer.imageryLayers.add(detail);
      this.detail = detail;
      this.detailStatus = "Denver / Front Range NAIP requested";
      let failedTiles = 0;
      detailProvider.errorEvent.addEventListener((error) => {
        error.retry = false;
        if (this.destroyed || this.detail !== detail) return;
        failedTiles += 1;
        if (failedTiles < 3 && error.error?.statusCode !== 429) {
          this.detailStatus =
            "Some NAIP tiles unavailable · background retained (Retry maps)";
          this.status();
          return;
        }
        console.warn("Denver / Front Range NAIP layer stopped:", error.message);
        queueMicrotask(() => {
          if (this.destroyed || this.detail !== detail) return;
          this.viewer.imageryLayers.remove(detail, true);
          this.detail = undefined;
          this.detailStatus =
            "NAIP detail unavailable · background retained (Retry map services)";
          this.status();
        });
      });
    } else if (imagery === "earth") {
      // NASA GIBS publishes this non-time-dynamic Blue Marble relief layer as
      // public WMTS imagery. It supplies a coherent global Earth view, but its
      // level-8 cache is visual context, not current satellite photography.
      const provider = new WebMapTileServiceImageryProvider({
        url: NASA_BLUE_MARBLE_URL,
        layer: "BlueMarble_ShadedRelief_Bathymetry",
        style: "default",
        tileMatrixSetID: "GoogleMapsCompatible_Level8",
        maximumLevel: 8,
        format: "image/jpeg",
        credit: new Credit(
          '<a href="https://earthdata.nasa.gov/eosdis/science-system-description/eosdis-components/gibs" target="_blank">NASA GIBS Blue Marble</a>',
          true,
        ),
        enablePickFeatures: false,
      });
      const layer = this.viewer.imageryLayers.addImageryProvider(provider);
      this.aerial = layer;
      this.imageryStatus = "NASA Earth relief · global visual context";
      provider.errorEvent.addEventListener(() => {
        queueMicrotask(() => {
          if (this.destroyed || this.aerial !== layer) return;
          this.viewer.imageryLayers.remove(layer, true);
          this.aerial = undefined;
          this.imageryStatus =
            "NASA Earth relief unavailable · offline background (Retry map services)";
          this.status();
        });
      });
    }
  }

  private setTerrain(enabled: boolean): void {
    this.terrain?.stop();
    this.terrain = undefined;
    this.viewer.terrainProvider = new EllipsoidTerrainProvider();
    this.terrainStatus = "flat surface";
    if (enabled) {
      const terrain = new OpenTerrainProvider(
        () => {
          if (this.destroyed || this.terrain !== terrain) return;
          this.terrainStatus =
            "world terrain · 3DEP detail unavailable (Retry maps)";
          this.status();
        },
        () => {
          if (this.destroyed || this.terrain !== terrain) return;
          this.terrainStatus = "world terrain · USGS 3DEP detail loaded";
          this.status();
        },
      );
      this.terrain = terrain;
      terrain.errorEvent.addEventListener((error: unknown) => {
        if (this.destroyed || this.terrain !== terrain) return;
        // Cesium reports rejected detail requests again after request3dep has
        // disabled detail and notified the UI. Let it upsample parent terrain;
        // only a base-terrain failure should replace the whole provider.
        if (
          error instanceof TileProviderError &&
          error.provider === terrain &&
          error.level > TERRAIN_MAX_LEVEL &&
          error.level <= TERRAIN_DETAIL_MAX_LEVEL
        ) {
          error.retry = false;
          return;
        }
        terrain.stop();
        this.terrain = undefined;
        // Defer provider replacement until Cesium's current tile operation finishes.
        queueMicrotask(() => {
          if (this.destroyed || this.viewer.terrainProvider !== terrain) return;
          this.viewer.terrainProvider = new EllipsoidTerrainProvider();
          this.terrainStatus =
            "Terrain unavailable · flat fallback (Retry maps)";
          this.status();
        });
      });
      this.viewer.terrainProvider = terrain;
      this.terrainStatus = "world terrain · 3DEP requested at close range";
    }
  }

  private async loadAirport(): Promise<void> {
    const source = await GeoJsonDataSource.load("/airports/den.geojson", {
      clampToGround: true,
    });
    if (this.destroyed) {
      source.entities.removeAll();
      return;
    }
    const now = this.viewer.clock.currentTime;
    // Suppress overlapping airport labels rather than replace them with counts.
    // Geometry remains visible; closer views reveal individual labels.
    source.clustering.enabled = true;
    source.clustering.pixelRange = 18;
    source.clustering.minimumClusterSize = 2;
    source.clustering.clusterBillboards = false;
    source.clustering.clusterPoints = false;
    source.clustering.clusterEvent.addEventListener((_entities, cluster) => {
      cluster.label.show = false;
      cluster.billboard.show = false;
      cluster.point.show = false;
    });
    // One label per named feature; taxiway/building labels appear only close up.
    const labelled = new Set<string>();
    for (const entity of source.entities.values) {
      const kind = String(entity.properties?.kind?.getValue(now));
      const name = String(entity.properties?.name?.getValue(now) ?? "");
      entity.name = `${kind}${name ? ` ${name}` : ""} · FAA reference`;
      const polygon = entity.polygon;
      if (!polygon) continue;
      polygon.material = new ColorMaterialProperty(
        Color.fromCssColorString(AIRPORT_COLORS[kind] ?? "#ffffff").withAlpha(
          kind === "runway" ? 0.32 : 0.18,
        ),
      );
      polygon.outline = new ConstantProperty(false);
      polygon.distanceDisplayCondition = new ConstantProperty(
        new DistanceDisplayCondition(0, 55_000),
      );
      const hierarchy = polygon.hierarchy?.getValue(now) as
        | PolygonHierarchy
        | undefined;
      const positions = hierarchy?.positions;
      if (
        positions &&
        name &&
        (kind === "runway" ||
          kind === "building" ||
          (kind === "taxiway" && /^[A-Z]{1,2}[0-9]?$/.test(name))) &&
        !labelled.has(`${kind}/${name}`)
      ) {
        labelled.add(`${kind}/${name}`);
        const center = new Cartesian3();
        for (const position of positions)
          Cartesian3.add(center, position, center);
        Cartesian3.divideByScalar(center, positions.length, center);
        entity.position = new ConstantPositionProperty(center);
        entity.label = new LabelGraphics({
          text:
            kind === "runway"
              ? `RWY ${name}`
              : kind === "taxiway"
                ? `TWY ${name}`
                : name,
          font: "12px monospace",
          fillColor: Color.WHITE,
          style: LabelStyle.FILL_AND_OUTLINE,
          outlineWidth: 3,
          outlineColor: Color.fromCssColorString("#10191f"),
          heightReference: HeightReference.CLAMP_TO_GROUND,
          pixelOffset: new Cartesian2(0, -8),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          distanceDisplayCondition: new DistanceDisplayCondition(
            0,
            kind === "runway" ? 45_000 : kind === "building" ? 7000 : 2500,
          ),
        });
      }
    }
    this.airport = source;
    source.show = this.preferences.airport;
    await this.viewer.dataSources.add(source);
    if (this.destroyed) return;
    this.viewer.cesiumWidget.creditDisplay.addStaticCredit(AIRPORT_CREDIT);
    this.status();
  }

  destroy(): void {
    this.destroyed = true;
    this.generation++;
    this.terrain?.stop();
    for (const layer of [this.detail, this.aerial, this.background]) {
      if (layer) this.viewer.imageryLayers.remove(layer, true);
    }
    this.detail = undefined;
    this.aerial = undefined;
    this.background = undefined;
  }
}
