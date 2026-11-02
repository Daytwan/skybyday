import {
  BoundingSphere,
  CallbackProperty,
  Cartesian2,
  Cartesian3,
  Math as CesiumMath,
  Color,
  ConstantPositionProperty,
  ConstantProperty,
  CustomDataSource,
  EllipsoidTerrainProvider,
  HeadingPitchRange,
  HeightReference,
  LabelStyle,
  NearFarScalar,
  PolylineArrowMaterialProperty,
  PolylineDashMaterialProperty,
  Viewer,
} from "cesium";
import { trackEndpoint } from "../aircraft/motion.ts";
import type { DisplayAircraft } from "../aircraft/state.ts";
import {
  symbolDirection,
  symbolImage,
  symbolKind,
} from "../aircraft/symbols.ts";
import type { CoverageRegion, Snapshot } from "../network/snapshot.ts";
import type { Route } from "../ui/route.ts";
import { LandmarksLayer } from "./landmarks.ts";
import { MapLayers } from "./maps.ts";
import { PlacesLayer } from "./places.ts";
import type { MapPreferences } from "./preferences.ts";
import { referenceEndpoints } from "./route-reference.ts";
import "cesium/Build/Cesium/Widgets/widgets.css";

type Area = Snapshot["tracking"]["area"];
export interface GlobeOptions {
  labels: "selected" | "all" | "none";
  tracks: boolean;
}
const ACCENT = Color.fromCssColorString("#9ae1cf");
const STALE = Color.fromCssColorString("#87949c");
const SURFACE = Color.fromCssColorString("#17272d");

export class AircraftGlobe {
  private readonly viewer: Viewer;
  private readonly maps: MapLayers;
  private readonly places: PlacesLayer;
  private readonly landmarks: LandmarksLayer;
  private readonly aircraft = new CustomDataSource("Aircraft observations");
  private readonly coverage = new CustomDataSource("Observed regions");
  private readonly routeReference = new CustomDataSource(
    "Published airport reference",
  );
  private coverageSignature = "";
  private referenceSignature = "";
  private referencePositions: Cartesian3[] = [];
  private readonly previous = new Map<string, DisplayAircraft>();
  private readonly trackPositions = new Map<string, Cartesian3[]>();
  private options: GlobeOptions = { labels: "selected", tracks: false };
  private stylesChanged = true;
  private area: Area = {
    latitude_deg: 39.8561,
    longitude_deg: -104.6737,
    radius_nm: 60,
  };
  private get reducedMotion(): boolean {
    return matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  constructor(
    container: HTMLElement,
    onSelect: (id: string | null) => void,
    onFailure: (message: string) => void,
    onMapStatus: (message: string) => void,
    onPlacesStatus: (message: string) => void,
    onLandmarksStatus: (message: string) => void,
  ) {
    this.viewer = new Viewer(container, {
      creditContainer: "map-credits",
      baseLayer: false,
      baseLayerPicker: false,
      terrainProvider: new EllipsoidTerrainProvider(),
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      animation: false,
      timeline: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: true,
      shouldAnimate: false,
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity,
      skyBox: false,
    });
    this.viewer.scene.globe.baseColor = SURFACE;
    this.maps = new MapLayers(this.viewer, onMapStatus);
    this.places = new PlacesLayer(this.viewer, onPlacesStatus);
    this.landmarks = new LandmarksLayer(this.viewer, onLandmarksStatus);
    this.viewer.scene.backgroundColor = Color.fromCssColorString("#0a1218");
    this.viewer.scene.screenSpaceCameraController.minimumZoomDistance = 300;
    this.viewer.scene.screenSpaceCameraController.maximumZoomDistance = 28_000_000;
    this.viewer.scene.renderError.addEventListener(() =>
      onFailure(
        "The globe could not render. Check WebGL support and reload. Aircraft data remains available in the list.",
      ),
    );
    void this.viewer.dataSources.add(this.aircraft);
    void this.viewer.dataSources.add(this.coverage);
    void this.viewer.dataSources.add(this.routeReference);
    this.viewer.selectedEntityChanged.addEventListener((entity) => {
      this.updateLabels();
      this.viewer.scene.requestRender();
      onSelect(
        entity && this.aircraft.entities.contains(entity) ? entity.id : null,
      );
    });
    this.setArea(this.area, true);
  }

  async configureMap(
    preferences: MapPreferences,
    retry = false,
  ): Promise<void> {
    await Promise.all([
      this.maps.apply(preferences, retry),
      this.places.setEnabled(preferences.places),
      this.landmarks.setEnabled(
        preferences.landmarks,
        preferences.imagery !== "offline",
      ),
    ]);
  }

  setArea(area: Area, initial = false): void {
    if (!initial && JSON.stringify(this.area) === JSON.stringify(area)) return;
    this.area = area;
    const entities = this.viewer.entities;
    entities.removeAll();
    const center = Cartesian3.fromDegrees(
      area.longitude_deg,
      area.latitude_deg,
    );
    const isDenver =
      Math.abs(area.latitude_deg - 39.8561) < 0.01 &&
      Math.abs(area.longitude_deg + 104.6737) < 0.01;
    entities.add({
      id: "area-center",
      position: center,
      point: {
        pixelSize: 4,
        color: Color.WHITE,
        heightReference: HeightReference.CLAMP_TO_GROUND,
      },
      label: {
        text: isDenver ? "DEN" : "AREA CENTER",
        font: "12px monospace",
        fillColor: Color.WHITE,
        style: LabelStyle.FILL_AND_OUTLINE,
        outlineColor: SURFACE,
        outlineWidth: 3,
        pixelOffset: new Cartesian2(0, 18),
        heightReference: HeightReference.CLAMP_TO_GROUND,
      },
      ellipse: {
        semiMajorAxis: area.radius_nm * 1852,
        semiMinorAxis: area.radius_nm * 1852,
        material: ACCENT.withAlpha(0.025),
        outline: false,
      },
    });
    this.home(initial);
  }
  home(immediate = false): void {
    const center = Cartesian3.fromDegrees(
      this.area.longitude_deg,
      this.area.latitude_deg,
    );
    this.viewer.camera.flyToBoundingSphere(new BoundingSphere(center, 1), {
      offset: new HeadingPitchRange(
        0,
        CesiumMath.toRadians(-65),
        Math.max(15_000, this.area.radius_nm * 1852 * 3.4),
      ),
      duration: immediate || this.reducedMotion ? 0 : 1.1,
    });
  }
  setCoverage(regions: CoverageRegion[]): void {
    const signature = JSON.stringify(
      regions.map(({ id, name, area }) => ({ id, name, area })),
    );
    if (signature === this.coverageSignature) return;
    this.coverageSignature = signature;
    this.coverage.entities.removeAll();
    const primary = this.viewer.entities.getById("area-center");
    if (primary) primary.show = regions.length <= 1;
    if (regions.length > 1)
      for (const region of regions) {
        this.coverage.entities.add({
          id: `coverage-${region.id}`,
          position: Cartesian3.fromDegrees(
            region.area.longitude_deg,
            region.area.latitude_deg,
          ),
          ellipse: {
            semiMajorAxis: region.area.radius_nm * 1852,
            semiMinorAxis: region.area.radius_nm * 1852,
            material: ACCENT.withAlpha(0.09),
            outline: false,
          },
          label: {
            text: `${region.id.toUpperCase()} · ${region.area.radius_nm} NM`,
            font: "12px monospace",
            fillColor: Color.WHITE,
            style: LabelStyle.FILL_AND_OUTLINE,
            outlineColor: SURFACE,
            outlineWidth: 3,
            heightReference: HeightReference.CLAMP_TO_GROUND,
            pixelOffset: new Cartesian2(0, 12),
          },
        });
      }
    this.viewer.scene.requestRender();
  }
  visitRegion(area: Area): void {
    this.viewer.trackedEntity = undefined;
    this.viewer.camera.flyToBoundingSphere(
      new BoundingSphere(
        Cartesian3.fromDegrees(area.longitude_deg, area.latitude_deg),
        1,
      ),
      {
        offset: new HeadingPitchRange(
          0,
          CesiumMath.toRadians(-65),
          area.radius_nm * 1852 * 3.4,
        ),
        duration: this.reducedMotion ? 0 : 1.1,
      },
    );
  }
  setReferenceRoute(route?: Route): void {
    const endpoints = referenceEndpoints(route);
    const signature = JSON.stringify(endpoints);
    if (signature === this.referenceSignature) return;
    this.referenceSignature = signature;
    this.routeReference.entities.removeAll();
    this.referencePositions =
      endpoints?.map((point) =>
        Cartesian3.fromDegrees(point.longitude, point.latitude),
      ) ?? [];
    if (endpoints) {
      this.routeReference.entities.add({
        polyline: {
          positions: this.referencePositions,
          width: 2,
          clampToGround: true,
          material: new PolylineDashMaterialProperty({
            color: Color.fromCssColorString("#f1ce94"),
            dashLength: 16,
          }),
        },
      });
      for (const [index, point] of endpoints.entries())
        this.routeReference.entities.add({
          position: this.referencePositions[index],
          point: {
            pixelSize: 7,
            color: Color.fromCssColorString("#f1ce94"),
            heightReference: HeightReference.CLAMP_TO_GROUND,
          },
          label: {
            text: `${point.code} · reference`,
            font: "12px monospace",
            fillColor: Color.WHITE,
            style: LabelStyle.FILL_AND_OUTLINE,
            outlineColor: SURFACE,
            outlineWidth: 3,
            pixelOffset: new Cartesian2(0, -18),
            heightReference: HeightReference.CLAMP_TO_GROUND,
          },
        });
    }
    this.viewer.scene.requestRender();
  }
  viewReference(): void {
    if (this.referencePositions.length !== 2) return;
    this.viewer.trackedEntity = undefined;
    const sphere = BoundingSphere.fromPoints(this.referencePositions);
    this.viewer.camera.flyToBoundingSphere(sphere, {
      offset: new HeadingPitchRange(
        0,
        -Math.PI / 2,
        Math.max(30_000, sphere.radius * 3.5),
      ),
      duration: this.reducedMotion ? 0 : 1.1,
    });
  }
  follow(id: string | null): void {
    this.viewer.trackedEntity = id
      ? this.aircraft.entities.getById(id)
      : undefined;
    this.viewer.scene.requestRender();
  }
  get following(): boolean {
    return this.viewer.trackedEntity !== undefined;
  }
  airport(): void {
    // Fixed DEN reference view. It does not change the backend's polling area.
    this.viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(-104.6737, 39.8561, 21_000),
      orientation: { heading: 0, pitch: -Math.PI / 2, roll: 0 },
      duration: this.reducedMotion ? 0 : 1.1,
    });
  }
  terminal(): void {
    this.viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(-104.6738, 39.8605, 6500),
      orientation: { heading: 0, pitch: -Math.PI / 2, roll: 0 },
      duration: this.reducedMotion ? 0 : 1.1,
    });
  }
  zoom(inward: boolean): void {
    const distance = Math.max(
      100,
      this.viewer.camera.positionCartographic.height * 0.35,
    );
    if (inward) this.viewer.camera.zoomIn(distance);
    else this.viewer.camera.zoomOut(distance);
    this.viewer.scene.requestRender();
  }
  terrainView(): void {
    const center = Cartesian3.fromDegrees(-105.58, 39.7, 3000);
    this.viewer.camera.flyToBoundingSphere(new BoundingSphere(center, 1), {
      offset: new HeadingPitchRange(
        CesiumMath.toRadians(-65),
        CesiumMath.toRadians(-25),
        45_000,
      ),
      duration: this.reducedMotion ? 0 : 1.1,
    });
  }
  skyline(): void {
    this.viewer.camera.flyToBoundingSphere(
      new BoundingSphere(Cartesian3.fromDegrees(-104.991, 39.747, 1600), 1),
      {
        offset: new HeadingPitchRange(
          CesiumMath.toRadians(25),
          CesiumMath.toRadians(-25),
          4800,
        ),
        duration: this.reducedMotion ? 0 : 1.1,
      },
    );
  }
  civicCenter(): void {
    // Camera target only; geometry heights come from the municipal scene.
    this.viewer.camera.flyToBoundingSphere(
      new BoundingSphere(Cartesian3.fromDegrees(-104.9896, 39.7397, 1600), 1),
      {
        offset: new HeadingPitchRange(
          CesiumMath.toRadians(-30),
          CesiumMath.toRadians(-30),
          550,
        ),
        duration: this.reducedMotion ? 0 : 1.1,
      },
    );
  }
  topDown(): void {
    this.viewer.camera.flyTo({
      destination: this.viewer.camera.positionWC.clone(),
      orientation: { heading: 0, pitch: -Math.PI / 2, roll: 0 },
      duration: this.reducedMotion ? 0 : 0.5,
    });
  }
  world(): void {
    this.viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(
        this.area.longitude_deg,
        this.area.latitude_deg,
        16_000_000,
      ),
      duration: this.reducedMotion ? 0 : 1.2,
    });
  }
  select(id: string | null, locate = false): void {
    const entity = id ? this.aircraft.entities.getById(id) : undefined;
    if (this.viewer.trackedEntity && this.viewer.trackedEntity !== entity)
      this.viewer.trackedEntity = undefined;
    this.viewer.selectedEntity = entity;
    if (locate && entity) {
      const position = entity.position?.getValue(this.viewer.clock.currentTime);
      if (position)
        this.viewer.camera.flyToBoundingSphere(
          new BoundingSphere(position, 1),
          {
            offset: new HeadingPitchRange(0, CesiumMath.toRadians(-55), 30_000),
            duration: this.reducedMotion ? 0 : 0.8,
          },
        );
    }
    this.viewer.scene.requestRender();
  }
  setOptions(options: GlobeOptions): void {
    if (
      this.options.labels === options.labels &&
      this.options.tracks === options.tracks
    )
      return;
    this.options = options;
    this.stylesChanged = true;
  }
  private updateLabels(): void {
    for (const entity of this.aircraft.entities.values) {
      if (entity.label)
        entity.label.show = new ConstantProperty(
          this.options.labels === "all" ||
            (this.options.labels === "selected" &&
              this.viewer.selectedEntity === entity),
        );
    }
  }
  update(planes: DisplayAircraft[]): number {
    const entities = this.aircraft.entities;
    const present = new Set<string>();
    let changed = false;
    entities.suspendEvents();
    try {
      for (const plane of planes) {
        const observation = plane.observation;
        const position = plane.display.position;
        if (!position) continue;
        present.add(observation.icao24);
        const old = this.previous.get(observation.icao24);
        const previousPosition = old?.display.position;
        const moved =
          !previousPosition ||
          previousPosition.longitude !== position.longitude ||
          previousPosition.latitude !== position.latitude ||
          previousPosition.height !== position.height;
        const restyled =
          this.stylesChanged ||
          !old ||
          old.observation !== observation ||
          old.stale !== plane.stale ||
          previousPosition?.projected !== position.projected;
        if (!moved && !restyled) continue;
        let entity = entities.getById(observation.icao24);
        if (!entity)
          entity = entities.add({
            id: observation.icao24,
            name: observation.callsign ?? observation.icao24,
            billboard: {
              image: symbolImage(symbolKind(observation.aircraft_type)),
              width: 34,
              height: 34,
              // Preserve detail up close without filling a regional/mobile view.
              scaleByDistance: new NearFarScalar(15_000, 1, 500_000, 0.28),
              disableDepthTestDistance: Number.POSITIVE_INFINITY,
            },
            position: new ConstantPositionProperty(),
            polyline: {
              // Dynamic positions avoid rebuilding asynchronous static geometry on
              // every interpolation frame. The callback reads our bounded cache.
              positions: new CallbackProperty(
                () => this.trackPositions.get(observation.icao24) ?? [],
                false,
              ),
              show: false,
              width: 6,
              material: new PolylineArrowMaterialProperty(
                ACCENT.withAlpha(0.8),
              ),
            },
            label: {
              text: "",
              show: false,
              font: "13px monospace",
              style: LabelStyle.FILL_AND_OUTLINE,
              outlineWidth: 3,
              outlineColor: SURFACE,
              pixelOffset: new Cartesian2(0, -22),
            },
          });
        if (moved)
          (entity.position as ConstantPositionProperty).setValue(
            Cartesian3.fromDegrees(
              position.longitude,
              position.latitude,
              position.height,
            ),
          );
        const color = plane.stale ? STALE : ACCENT;
        if (restyled && entity.billboard) {
          const direction = symbolDirection(observation);
          entity.billboard.image = new ConstantProperty(
            symbolImage(
              symbolKind(observation.aircraft_type),
              position.projected,
              plane.stale,
              direction.degrees === null,
            ),
          );
          entity.billboard.rotation = new ConstantProperty(
            direction.degrees === null
              ? 0
              : -CesiumMath.toRadians(direction.degrees),
          );
          entity.billboard.alignedAxis = new ConstantProperty(
            direction.degrees === null ? Cartesian3.ZERO : Cartesian3.UNIT_Z,
          );
          entity.billboard.heightReference = new ConstantProperty(
            position.projected
              ? HeightReference.CLAMP_TO_GROUND
              : HeightReference.NONE,
          );
        }
        if (restyled && entity.label) {
          entity.name = observation.callsign ?? observation.icao24;
          entity.label.text = new ConstantProperty(entity.name);
          entity.label.fillColor = new ConstantProperty(color);
          entity.label.heightReference = new ConstantProperty(
            position.projected
              ? HeightReference.CLAMP_TO_GROUND
              : HeightReference.NONE,
          );
          entity.label.disableDepthTestDistance = new ConstantProperty(
            Number.POSITIVE_INFINITY,
          );
          entity.label.show = new ConstantProperty(
            this.options.labels === "all" ||
              (this.options.labels === "selected" &&
                this.viewer.selectedEntity === entity),
          );
        }
        if (entity.polyline) {
          const end =
            this.options.tracks && !plane.stale
              ? trackEndpoint(position, observation.ground_track_deg)
              : null;
          entity.polyline.show = new ConstantProperty(end !== null);
          entity.polyline.clampToGround = new ConstantProperty(
            position.projected,
          );
          if (end)
            this.trackPositions.set(observation.icao24, [
              Cartesian3.fromDegrees(
                position.longitude,
                position.latitude,
                position.height,
              ),
              Cartesian3.fromDegrees(end.longitude, end.latitude, end.height),
            ]);
        }
        this.previous.set(observation.icao24, plane);
        changed = true;
      }
      for (const id of this.previous.keys()) {
        if (!present.has(id)) {
          entities.removeById(id);
          this.previous.delete(id);
          this.trackPositions.delete(id);
          changed = true;
        }
      }
    } finally {
      entities.resumeEvents();
    }
    this.stylesChanged = false;
    if (changed) this.viewer.scene.requestRender();
    return present.size;
  }
  destroy(): void {
    this.places.destroy();
    this.landmarks.destroy();
    this.maps.destroy();
    this.viewer.destroy();
  }
}
