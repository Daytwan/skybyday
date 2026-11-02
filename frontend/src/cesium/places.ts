import {
  Cartesian2,
  Cartesian3,
  Math as CesiumMath,
  Color,
  Credit,
  CustomDataSource,
  Ellipsoid,
  EllipsoidalOccluder,
  HeightReference,
  LabelStyle,
  SceneTransforms,
  type Viewer,
} from "cesium";
import {
  type Bounds,
  inBounds,
  type Place,
  parsePlaces,
  placeChunks,
} from "./places-data.ts";

/** Static local gazetteer; bounded labels and an LRU of geographic chunks. */
export class PlacesLayer {
  private readonly source = new CustomDataSource("GeoNames places");
  private readonly cache = new Map<string, Place[]>();
  private major: Place[] = [];
  private keys = new Set<string>();
  private controller?: AbortController;
  private generation = 0;
  private enabled = false;
  private ready = false;
  private destroyed = false;
  private readonly unlisten: () => void;

  private readonly viewer: Viewer;
  private readonly report: (text: string) => void;
  constructor(viewer: Viewer, report: (text: string) => void) {
    this.viewer = viewer;
    this.report = report;
    void viewer.dataSources.add(this.source);
    this.unlisten = viewer.camera.moveEnd.addEventListener(
      () => void this.update(),
    );
    viewer.cesiumWidget.creditDisplay.addStaticCredit(
      new Credit(
        '<a href="/map-attribution.html" target="_blank">GeoNames</a>',
        true,
      ),
    );
  }

  async setEnabled(enabled: boolean): Promise<void> {
    this.enabled = enabled;
    this.source.show = enabled;
    await this.update();
  }

  private async json(path: string, signal: AbortSignal): Promise<unknown> {
    const response = await fetch(`/places/${path}.json`, { signal });
    if (!response.ok) throw new Error("Place labels unavailable");
    const text = await response.text();
    if (text.length > 5_000_000) throw new Error("Place file too large");
    return JSON.parse(text);
  }

  private async update(): Promise<void> {
    const generation = ++this.generation;
    this.controller?.abort();
    if (this.destroyed || !this.enabled) return;
    const controller = new AbortController();
    this.controller = controller;
    const deadline = setTimeout(() => controller.abort(), 8000);
    try {
      if (!this.ready) {
        const [manifest, major] = await Promise.all([
          this.json("manifest", controller.signal),
          this.json("major", controller.signal),
        ]);
        if (
          !manifest ||
          typeof manifest !== "object" ||
          !("chunks" in manifest) ||
          !manifest.chunks ||
          typeof manifest.chunks !== "object"
        )
          throw new Error("Invalid place index");
        this.keys = new Set(Object.keys(manifest.chunks));
        this.major = parsePlaces(major);
        this.ready = true;
      }
      const rectangle = this.viewer.camera.computeViewRectangle();
      const bounds: Bounds | undefined = rectangle && {
        west: CesiumMath.toDegrees(rectangle.west),
        east: CesiumMath.toDegrees(rectangle.east),
        south: CesiumMath.toDegrees(rectangle.south),
        north: CesiumMath.toDegrees(rectangle.north),
      };
      const nearby =
        bounds && this.viewer.camera.positionCartographic.height < 1_500_000
          ? placeChunks(bounds).filter((key) => this.keys.has(key))
          : [];
      const places = new Map(this.major.map((place) => [place[0], place]));
      // Sequential chunk requests keep demand bounded even while users move the camera.
      for (const key of nearby) {
        let data = this.cache.get(key);
        if (!data) data = parsePlaces(await this.json(key, controller.signal));
        this.cache.delete(key);
        this.cache.set(key, data);
        if (this.cache.size > 12)
          this.cache.delete(this.cache.keys().next().value as string);
        for (const place of data)
          if (!bounds || inBounds(place, bounds)) places.set(place[0], place);
      }
      if (this.destroyed || generation !== this.generation) return;
      this.draw([...places.values()]);
      this.report("City labels · zoom in for smaller places");
    } catch {
      if (!this.destroyed && generation === this.generation)
        this.report("Place labels unavailable · toggle labels to retry");
    } finally {
      clearTimeout(deadline);
    }
  }

  private draw(places: Place[]): void {
    const scene = this.viewer.scene;
    const occluder = new EllipsoidalOccluder(
      Ellipsoid.WGS84,
      this.viewer.camera.positionWC,
    );
    const boxes: { x: number; y: number; width: number }[] = [];
    this.source.entities.suspendEvents();
    this.source.entities.removeAll();
    places.sort((a, b) => b[4] - a[4]);
    for (const place of places) {
      if (boxes.length >= 180) break;
      const position = Cartesian3.fromDegrees(place[2], place[3]);
      if (!occluder.isPointVisible(position)) continue;
      const screen = SceneTransforms.worldToWindowCoordinates(scene, position);
      if (
        !screen ||
        screen.x < 30 ||
        screen.y < 30 ||
        screen.x > scene.canvas.clientWidth - 30 ||
        screen.y > scene.canvas.clientHeight - 30
      )
        continue;
      const width = Math.min(220, place[1].length * 7 + 20);
      if (
        boxes.some(
          (box) =>
            Math.abs(box.y - screen.y) < 26 &&
            Math.abs(box.x - screen.x) < (box.width + width) / 2,
        )
      )
        continue;
      boxes.push({ x: screen.x, y: screen.y, width });
      this.source.entities.add({
        id: `place-${place[0]}`,
        position,
        label: {
          text: place[1],
          font: `${place[4] > 500_000 ? 14 : 12}px sans-serif`,
          fillColor: Color.fromCssColorString("#f4f0e4"),
          style: LabelStyle.FILL_AND_OUTLINE,
          outlineColor: Color.fromCssColorString("#19252b"),
          outlineWidth: 3,
          heightReference: HeightReference.CLAMP_TO_GROUND,
          pixelOffset: new Cartesian2(0, -6),
        },
      });
    }
    this.source.entities.resumeEvents();
    scene.requestRender();
  }

  destroy(): void {
    this.destroyed = true;
    this.controller?.abort();
    this.unlisten();
    this.cache.clear();
  }
}
