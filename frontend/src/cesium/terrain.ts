import {
  Math as CesiumMath,
  Credit,
  CustomHeightmapTerrainProvider,
  HeightmapEncoding,
  HeightmapTerrainData,
  type Request,
  RequestState,
  Resource,
  type TerrainData,
  WebMercatorTilingScheme,
} from "cesium";
import {
  geoidUndulation,
  intersectsTerrainDetail,
  meshAlignedExportRectangle,
  TERRAIN_DETAIL_MAX_LEVEL,
  TERRAIN_DETAIL_MIN_LEVEL,
  TERRAIN_MAX_LEVEL,
  TERRAIN_SAMPLES,
  terrariumHeights,
} from "./elevation.ts";

const THREE_DEP_URL =
  "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage";
const MAX_TERRAIN_BYTES = 2 * 1024 * 1024;
const REQUEST_TIMEOUT_MS = 15_000;

export function build3depExportUrl(rectangle: {
  west: number;
  south: number;
  east: number;
  north: number;
}): string {
  const aligned = meshAlignedExportRectangle(rectangle);
  const query = new URLSearchParams({
    bbox: `${aligned.west},${aligned.south},${aligned.east},${aligned.north}`,
    bboxSR: "3857",
    imageSR: "3857",
    size: `${TERRAIN_SAMPLES},${TERRAIN_SAMPLES}`,
    format: "lerc",
    pixelType: "F32",
    interpolation: "RSP_BilinearInterpolation",
    f: "image",
  });
  return `${THREE_DEP_URL}?${query}`;
}

function isLerc(buffer: ArrayBuffer): boolean {
  if (buffer.byteLength < 24) return false;
  const signature = new TextDecoder("ascii").decode(
    new Uint8Array(buffer, 0, Math.min(10, buffer.byteLength)),
  );
  return signature.startsWith("CntZImage") || signature.startsWith("Lerc2");
}

/**
 * Public Mapzen/AWS Terrarium tiles. Cesium schedules/cancels viewport requests.
 * Limit detail and stop on service failures; no automatic retry loop or prefetch.
 */
export class OpenTerrainProvider extends CustomHeightmapTerrainProvider {
  private stopped = false;
  private detailStopped = false;
  private readonly pending = new Set<Request>();
  private readonly detailFailure?: (message: string) => void;
  private readonly detailLoaded?: () => void;
  private hasLoadedDetail = false;

  constructor(
    detailFailure?: (message: string) => void,
    detailLoaded?: () => void,
  ) {
    super({
      width: TERRAIN_SAMPLES,
      height: TERRAIN_SAMPLES,
      tilingScheme: new WebMercatorTilingScheme(),
      callback: () => undefined,
      credit: new Credit(
        '<a href="/map-attribution.html" target="_blank">Mapzen · USGS 3DEP · NOAA geoid</a>',
        true,
      ),
    });
    this.detailFailure = detailFailure;
    this.detailLoaded = detailLoaded;
  }

  override requestTileGeometry(
    x: number,
    y: number,
    level: number,
    request?: Request,
  ): Promise<TerrainData> | undefined {
    if (this.stopped) return undefined;
    if (this.isDetailTile(x, y, level))
      return this.request3dep(x, y, level, request);
    if (level > TERRAIN_MAX_LEVEL) return undefined;
    return this.requestTerrarium(x, y, level, request);
  }

  private requestTerrarium(
    x: number,
    y: number,
    level: number,
    request?: Request,
  ): Promise<TerrainData> | undefined {
    const resource = new Resource({
      url: `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${level}/${x}/${y}.png`,
      request,
    });
    const promise = resource.fetchImage({
      preferBlob: true,
      preferImageBitmap: true,
    });
    if (!promise) return undefined;
    if (request) this.pending.add(request);
    // A hanging tile must not leave the UI claiming that terrain loaded.
    let timer: ReturnType<typeof setTimeout>;
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        if (!this.stopped)
          this.errorEvent.raiseEvent(new Error("Terrain request timed out"));
        request?.cancelFunction?.();
        reject(new Error("Terrain request timed out"));
      }, REQUEST_TIMEOUT_MS);
    });
    return Promise.race([promise, deadline])
      .then((image) => {
        if (image.width !== 256 || image.height !== 256)
          throw new Error("Unexpected terrain tile size");
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 256;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) throw new Error("Terrain decoder unavailable");
        context.drawImage(image, 0, 0);
        const heights = terrariumHeights(
          context.getImageData(0, 0, 256, 256).data,
          256,
          256,
        );
        if (image instanceof ImageBitmap) image.close();
        return new HeightmapTerrainData({
          buffer: heights,
          width: TERRAIN_SAMPLES,
          height: TERRAIN_SAMPLES,
          childTileMask: this.childTileMask(x, y, level),
        });
      })
      .catch((error: unknown) => {
        if (!this.stopped && request?.state !== RequestState.CANCELLED)
          this.errorEvent.raiseEvent(error);
        throw error;
      })
      .finally(() => {
        clearTimeout(timer);
        if (request) this.pending.delete(request);
      });
  }

  private request3dep(
    x: number,
    y: number,
    level: number,
    request?: Request,
  ): Promise<TerrainData> | undefined {
    const nativeRectangle = this.tilingScheme.tileXYToNativeRectangle(
      x,
      y,
      level,
    );
    const geographic = this.tilingScheme.tileXYToRectangle(x, y, level);
    const centerLongitude = CesiumMath.toDegrees(
      (geographic.west + geographic.east) / 2,
    );
    const centerLatitude = CesiumMath.toDegrees(
      (geographic.south + geographic.north) / 2,
    );
    const geoidOffset = geoidUndulation(centerLongitude, centerLatitude);
    if (geoidOffset === undefined) {
      this.stopDetail("3DEP datum correction is unavailable outside its grid");
      return undefined;
    }
    const resource = new Resource({
      url: build3depExportUrl(nativeRectangle),
      request,
    });
    const promise = resource.fetchArrayBuffer();
    if (!promise) return undefined;
    if (request) this.pending.add(request);
    let timer: ReturnType<typeof setTimeout>;
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        request?.cancelFunction?.();
        reject(new Error("3DEP terrain request timed out"));
      }, REQUEST_TIMEOUT_MS);
    });
    return Promise.race([promise, deadline])
      .then((buffer) => {
        if (buffer.byteLength > MAX_TERRAIN_BYTES)
          throw new Error("3DEP terrain response exceeds 2 MiB");
        if (!isLerc(buffer))
          throw new Error("3DEP did not return LERC terrain");
        if (!this.hasLoadedDetail) {
          this.hasLoadedDetail = true;
          this.detailLoaded?.();
        }
        return new HeightmapTerrainData({
          buffer: new Uint8Array(buffer),
          width: TERRAIN_SAMPLES,
          height: TERRAIN_SAMPLES,
          encoding: HeightmapEncoding.LERC,
          // 3DEP is NAVD88 orthometric height. NGS GEOID18 gives
          // h_NAD83(2011) = H_NAVD88 + N. Cesium's WGS84 frame still carries
          // the documented frame/epoch approximation; no offset is hidden.
          structure: { heightOffset: geoidOffset },
          childTileMask: this.childTileMask(x, y, level),
        });
      })
      .catch((error: unknown) => {
        if (!this.stopped && request?.state !== RequestState.CANCELLED) {
          const message =
            error instanceof Error ? error.message : String(error);
          this.stopDetail(message);
        }
        throw error;
      })
      .finally(() => {
        clearTimeout(timer);
        if (request) this.pending.delete(request);
      });
  }

  private stopDetail(message: string): void {
    if (this.detailStopped) return;
    this.detailStopped = true;
    this.detailFailure?.(message);
  }

  private isDetailTile(x: number, y: number, level: number): boolean {
    if (
      this.detailStopped ||
      level < TERRAIN_DETAIL_MIN_LEVEL ||
      level > TERRAIN_DETAIL_MAX_LEVEL
    )
      return false;
    const rectangle = this.tilingScheme.tileXYToRectangle(x, y, level);
    const correction = geoidUndulation(
      CesiumMath.toDegrees((rectangle.west + rectangle.east) / 2),
      CesiumMath.toDegrees((rectangle.south + rectangle.north) / 2),
    );
    return (
      correction !== undefined &&
      intersectsTerrainDetail(
        CesiumMath.toDegrees(rectangle.west),
        CesiumMath.toDegrees(rectangle.south),
        CesiumMath.toDegrees(rectangle.east),
        CesiumMath.toDegrees(rectangle.north),
      )
    );
  }

  private childTileMask(x: number, y: number, level: number): number {
    if (level < TERRAIN_MAX_LEVEL) return 15;
    if (level >= TERRAIN_DETAIL_MAX_LEVEL || this.detailStopped) return 0;
    let mask = 0;
    // Cesium bits: southwest=1, southeast=2, northwest=4, northeast=8.
    const children = [
      [x * 2, y * 2 + 1, 1],
      [x * 2 + 1, y * 2 + 1, 2],
      [x * 2, y * 2, 4],
      [x * 2 + 1, y * 2, 8],
    ] as const;
    for (const [childX, childY, bit] of children) {
      if (this.isDetailTile(childX, childY, level + 1)) mask |= bit;
    }
    return mask;
  }

  override getTileDataAvailable(x: number, y: number, level: number): boolean {
    return level <= TERRAIN_MAX_LEVEL || this.isDetailTile(x, y, level);
  }

  stop(): void {
    this.stopped = true;
    for (const request of this.pending) request.cancelFunction?.();
    this.pending.clear();
  }
}
