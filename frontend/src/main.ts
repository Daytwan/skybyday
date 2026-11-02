import { filterAircraft } from "./aircraft/filters.ts";
import { HistoricalReplay } from "./aircraft/replay.ts";
import {
  AircraftState,
  type DisplayAircraft,
  displayPosition,
} from "./aircraft/state.ts";
import {
  SYMBOL_NAMES,
  symbolDirection,
  symbolImage,
  symbolKind,
} from "./aircraft/symbols.ts";
import { AircraftGlobe, type GlobeOptions } from "./cesium/globe.ts";
import {
  readMapPreferences,
  saveMapPreferences,
} from "./cesium/preferences.ts";
import { referenceEndpoints } from "./cesium/route-reference.ts";
import {
  fetchHistory,
  type HistoryResponse,
  normalizeHistoryRange,
} from "./network/history.ts";
import { FleetRoutes, routeLabel } from "./network/routes.ts";
import { fetchSnapshot, type Snapshot } from "./network/snapshot.ts";
import { SnapshotStream } from "./network/stream.ts";
import {
  type DisplayPreferences,
  heightLabel,
  readDisplayPreferences,
  saveDisplayPreferences,
  speedLabel,
} from "./ui/preferences.ts";
import { RoutePanel } from "./ui/route.ts";
import { WeatherPanel } from "./ui/weather.ts";
import "./styles.css";

function element<T extends HTMLElement>(selector: string): T {
  const match = document.querySelector<T>(selector);
  if (!match) throw new Error(`Missing UI element: ${selector}`);
  return match;
}
const fleetRoutes = new FleetRoutes(() => {
  previousRoster = "";
  if (!disposed) render();
});
const route = new RoutePanel(element("#inspector"), (icao, callsign) =>
  fleetRoutes.get(icao, callsign),
);
const weather = new WeatherPanel(element("#panel-weather"));
element<HTMLButtonElement>("#weather-refresh").onclick = () =>
  void weather.refresh();
const state = new AircraftState();
const connection = element("#connection-status");
const roster = element("#aircraft-list");
const title = element("#selection-title");
const note = element("#selection-note");
const detail = element("#aircraft-detail");
const locate = element<HTMLButtonElement>("#locate");
const clear = element<HTMLButtonElement>("#clear-selection");
const follow = element<HTMLButtonElement>("#follow");
const viewReference = element<HTMLButtonElement>("#view-reference");
const coverageView = element<HTMLSelectElement>("#coverage-view");
const interpolation = element<HTMLInputElement>("#interpolate");
const altitude = element<HTMLInputElement>("#show-altitude");
const tracks = element<HTMLInputElement>("#show-tracks");
const labels = element<HTMLSelectElement>("#labels");
const query = element<HTMLInputElement>("#aircraft-filter");
const freshOnly = element<HTMLInputElement>("#fresh-only");
const mapImagery = element<HTMLSelectElement>("#map-imagery");
const mapTerrain = element<HTMLInputElement>("#map-terrain");
const mapPlaces = element<HTMLInputElement>("#map-places");
const mapLandmarks = element<HTMLInputElement>("#map-landmarks");
const mapAirport = element<HTMLInputElement>("#map-airport");
const units = element<HTMLSelectElement>("#units");
const displayPreferences = readDisplayPreferences();
units.value = displayPreferences.units;
labels.value = displayPreferences.labels;
altitude.checked = displayPreferences.altitude;
tracks.checked = displayPreferences.tracks;
const mapPreferences = readMapPreferences();
mapImagery.value = mapPreferences.imagery;
mapTerrain.checked = mapPreferences.terrain;
mapAirport.checked = mapPreferences.airport;
mapPlaces.checked = mapPreferences.places;
mapLandmarks.checked = mapPreferences.landmarks;
mapTerrain.disabled = mapPreferences.imagery === "offline";
const historyForm = element<HTMLFormElement>("#history-form");
const historyStart = element<HTMLInputElement>("#history-start");
const historyEnd = element<HTMLInputElement>("#history-end");
const historySource = element<HTMLSelectElement>("#history-source");
const historyIcao = element<HTMLInputElement>("#history-icao");
const historyStatus = element<HTMLElement>("#history-status");
const historyPlayback = element<HTMLElement>("#history-playback");
const historyClock = element<HTMLElement>("#history-clock");
const historySlider = element<HTMLInputElement>("#history-slider");
const historyRange = element<HTMLElement>("#history-range");
const historyPlay = element<HTMLButtonElement>("#history-play");
const historyLive = element<HTMLButtonElement>("#history-live");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
interpolation.checked =
  displayPreferences.interpolate && !reducedMotion.matches;
const replay = new HistoricalReplay();
let animationFrame: number | null = null;
let lastFrame = 0;
let totalAircraft = 0;
let selected: string | null = null;
let previousRoster = "";
let planes: DisplayAircraft[] = [];
let globe: AircraftGlobe | null = null;
let disposed = false;
let pageHidden = false;
let historyMode = false;
let historyPlaying = false;
let historyRequest = 0;
let historyAbort: AbortController | null = null;
let availabilityAbort: AbortController | null = null;

function showPanel(name: string, focus = false): void {
  for (const tab of document.querySelectorAll<HTMLButtonElement>(
    "[data-panel]",
  )) {
    const active = tab.dataset.panel === name;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    element(`#panel-${tab.dataset.panel}`).hidden = !active;
    if (active && focus) tab.focus();
  }
  if (name === "replay") void checkRecording();
  if (name === "weather") void weather.refresh();
}
const tabs = [...document.querySelectorAll<HTMLButtonElement>("[data-panel]")];
for (const [index, tab] of tabs.entries()) {
  tab.onclick = () => showPanel(tab.dataset.panel ?? "traffic");
  tab.addEventListener("keydown", (event) => {
    const offset =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const target =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : (index + offset + tabs.length) % tabs.length;
    if (!offset && !["Home", "End"].includes(event.key)) return;
    event.preventDefault();
    showPanel(tabs[target]?.dataset.panel ?? "traffic", true);
  });
}
async function checkRecording(): Promise<void> {
  availabilityAbort?.abort();
  const controller = new AbortController();
  availabilityAbort = controller;
  const deadline = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/api/history/status", {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Recording status unavailable");
    const value: unknown = await response.json();
    if (disposed || availabilityAbort !== controller) return;
    if (
      !value ||
      typeof value !== "object" ||
      !("enabled" in value) ||
      typeof value.enabled !== "boolean"
    )
      throw new Error("Invalid recording status");
    const status = value as { enabled: boolean; state?: string };
    element("#history-availability").textContent = !status.enabled
      ? "Recording is off. Enable optional local history storage to save and replay flights."
      : status.state === "available"
        ? "Local recording is enabled. Only observations saved by this instance can be replayed."
        : `Local recording is enabled but ${status.state === "degraded" ? "unavailable" : "not ready"}. Check the database connection.`;
  } catch {
    if (!disposed && availabilityAbort === controller)
      element("#history-availability").textContent =
        "Recording status unavailable. Reopen this panel to check again.";
  } finally {
    clearTimeout(deadline);
  }
}
function renderFeedNotice(): void {
  const snapshot = state.snapshot;
  const notice = element("#feed-notice");
  const connected = connection.dataset.connected === "true";
  let message = "";
  if (!connected)
    message = snapshot
      ? "Connection interrupted. Last-known positions keep aging while we reconnect."
      : "Connecting to the aircraft service…";
  else if (snapshot?.poller_status !== "running")
    message =
      "Aircraft polling has stopped. Check the local backend; cached observations keep aging.";
  else if (snapshot.data.status === "idle")
    message = "Waiting for the first provider response…";
  else if (snapshot.data.status !== "healthy") {
    const retry = snapshot.data.retry_in_seconds;
    const seconds =
      retry === null
        ? null
        : Math.max(
            0,
            Math.ceil(retry - state.elapsedSeconds(performance.now())),
          );
    message =
      snapshot.data.status === "suspended"
        ? "Live provider unavailable. Polling is suspended; check the service connection details."
        : `Live feed delayed. ${snapshot.data.aircraft.length ? "Showing last-known observations." : "No aircraft observations available yet."}${seconds === null ? "" : ` Next attempt in about ${seconds}s.`}`;
  }
  notice.textContent = message;
  notice.hidden = !message || historyMode;
  element("#feed-dot").dataset.state =
    connected && snapshot?.data.status === "healthy" ? "healthy" : "warning";
}

function globeFailure(message: string): void {
  const error = element("#map-error");
  error.textContent = message;
  error.hidden = false;
}

function utcLabel(milliseconds: number | null): string {
  return milliseconds === null
    ? "Unknown"
    : `${new Date(milliseconds).toISOString().replace("T", " ").replace("Z", " UTC")}`;
}

function datetimeInputValue(milliseconds: number): string {
  return new Date(milliseconds).toISOString().slice(0, 19);
}

function setDefaultHistoryRange(): void {
  const end = Date.now();
  historyStart.value = datetimeInputValue(end - 60 * 60 * 1000);
  historyEnd.value = datetimeInputValue(end);
}

function historySourceName(source: "synthetic" | "adsblol"): string {
  return source === "synthetic" ? "Synthetic · demo data" : "Live · ADSB.lol";
}

function setHistoryStatus(message: string, error = false): void {
  historyStatus.textContent = message;
  historyStatus.dataset.error = String(error);
}

function renderHistoricalSource(history: HistoryResponse): void {
  element("#source-label").textContent =
    `Replay · ${history.source === "adsblol" ? "ADSB.lol" : "synthetic"}`;
  const count = history.observations.length;
  element("#source-status").textContent =
    `${utcLabel(Date.parse(history.start))} → ${utcLabel(Date.parse(history.end))} · ${count} observations`;
  const credit = element("#source-credit");
  if (history.attribution) {
    const source = document.createElement("a");
    source.href = history.attribution.url ?? "#";
    source.textContent = history.attribution.attribution;
    if (!history.attribution.url) source.removeAttribute("href");
    const license = document.createElement("a");
    license.textContent = history.attribution.license;
    if (history.attribution.license_url)
      license.href = history.attribution.license_url;
    credit.replaceChildren(
      "Historical aircraft data: ",
      source,
      " · ",
      license,
    );
  } else {
    credit.textContent = `Historical ${history.source} aircraft data · attribution unavailable`;
  }
}

function renderLiveSource(snapshot: Snapshot): void {
  element("#source-label").textContent = historySourceName(
    snapshot.data.source,
  );
  element("#source-status").textContent =
    snapshot.poller_status !== "running"
      ? `Poller ${snapshot.poller_status}`
      : `Provider ${snapshot.data.status}${snapshot.data.last_error ? ` · ${snapshot.data.last_error}` : ""}`;
  const credit = element("#source-credit");
  if (snapshot.data.source === "adsblol") {
    const source = document.createElement("a");
    source.href = "https://www.adsb.lol/";
    source.textContent = "ADSB.lol";
    const license = document.createElement("a");
    license.href = "https://opendatacommons.org/licenses/odbl/1-0/";
    license.textContent = "ODbL 1.0";
    credit.replaceChildren("Aircraft data: ", source, " · ", license);
  } else credit.textContent = "Synthetic SkybyDay data · not real aircraft";
}

function renderHistoryControls(): void {
  historyPlayback.hidden = !historyMode;
  if (!historyMode || !replay.loaded) return;
  const { start, end } = replay.timeline;
  historySlider.min = String(start);
  historySlider.max = String(end);
  historySlider.value = String(replay.currentTime);
  historyClock.textContent = `${utcLabel(replay.currentTime)}`;
  historyRange.textContent = `${utcLabel(start)} → ${utcLabel(end)}`;
  historyPlay.textContent = historyPlaying ? "Pause" : "Play";
  historyPlay.disabled =
    replay.currentTime >= end || replay.response?.observations.length === 0;
}
try {
  globe = new AircraftGlobe(
    element("#globe"),
    (id) => selectAircraft(id, true),
    globeFailure,
    (message) => {
      element("#map-status").textContent = message;
    },
    (message) => {
      element("#places-status").textContent = message;
    },
    (message) => {
      element("#landmarks-status").textContent = message;
    },
  );
  void globe
    .configureMap(mapPreferences)
    .catch(() =>
      globeFailure(
        "Map imagery is unavailable. Aircraft still appear on the globe.",
      ),
    );
} catch (error) {
  console.error("Globe initialization failed", error);
  globeFailure(
    "The 3D globe is unavailable. Enable WebGL in a supported browser and reload. Aircraft observations remain available in the list.",
  );
  element<HTMLButtonElement>("#home").disabled = true;
  element<HTMLButtonElement>("#world").disabled = true;
  for (const id of [
    "#airport-view",
    "#north-up",
    "#terrain-view",
    "#terminal-view",
    "#civic-view",
    "#zoom-in",
    "#zoom-out",
    "#retry-maps",
  ])
    element<HTMLButtonElement>(id).disabled = true;
}

function selectAircraft(id: string | null, fromGlobe = false): void {
  if (id !== selected) element("#inspector").scrollTop = 0;
  selected = id;
  if (id) {
    historyIcao.value = id;
    if (matchMedia("(max-width: 760px)").matches && !fromGlobe)
      element(".map").scrollIntoView({ block: "start", behavior: "instant" });
  }
  if (!fromGlobe) globe?.select(id);
  renderSelection();
  for (const button of roster.querySelectorAll("button"))
    button.setAttribute("aria-pressed", String(button.dataset.icao === id));
}
function quantity(value: number | null, unit: string, decimals = 0): string {
  return value === null ? "Unknown" : `${value.toFixed(decimals)} ${unit}`;
}
function renderSelection(): void {
  const entry = planes.find((plane) => plane.observation.icao24 === selected);
  element("#inspector").hidden = !entry;
  detail.hidden = !entry;
  clear.hidden = !entry;
  locate.hidden = !entry;
  follow.hidden = !entry;
  if (!entry) {
    void route.update(null, null, false);
    globe?.setReferenceRoute();
    viewReference.hidden = true;
    element("#route-map-note").hidden = true;
    title.textContent = "Select an aircraft";
    note.textContent = selected
      ? "This aircraft has left the current snapshot or expired."
      : "Choose a marker or an aircraft below.";
    return;
  }
  const aircraft = entry.observation;
  const reference = historyMode
    ? undefined
    : fleetRoutes.get(aircraft.icao24, aircraft.callsign);
  globe?.setReferenceRoute(reference);
  const hasReferenceGeometry = !!referenceEndpoints(reference);
  viewReference.hidden = !hasReferenceGeometry;
  element("#route-map-note").hidden = !hasReferenceGeometry;
  follow.setAttribute("aria-pressed", String(globe?.following ?? false));
  follow.textContent = globe?.following ? "Stop following" : "Follow aircraft";
  void route.update(aircraft.icao24, aircraft.callsign, historyMode);
  const selectedUnits = units.value as DisplayPreferences["units"];
  const symbol = element<HTMLImageElement>("#selection-symbol");
  const kind = symbolKind(aircraft.aircraft_type);
  symbol.src = symbolImage(kind, false, entry.stale);
  element("#selection-subtitle").textContent =
    `${aircraft.icao24.toUpperCase()} · ${aircraft.aircraft_type ?? "Type unknown"}`;
  element("#selection-state").textContent = historyMode
    ? "HISTORICAL OBSERVATION"
    : entry.stale
      ? "LAST-KNOWN OBSERVATION"
      : "AIRCRAFT OBSERVATION";
  element("#metric-altitude").textContent = heightLabel(
    aircraft.barometric_altitude_m,
    selectedUnits,
  );
  element("#metric-speed").textContent = speedLabel(
    aircraft.groundspeed_mps,
    selectedUnits,
  );
  element("#metric-track").textContent = quantity(
    aircraft.ground_track_deg,
    "°",
    0,
  );
  element("#metric-age").textContent = aircraft.position_observed_at
    ? `${Math.floor(entry.ageSeconds)} s`
    : "Unknown";
  if (!historyMode && selected) historySource.value = aircraft.source;
  const position = entry.display.position;
  const observed = displayPosition(aircraft);
  title.textContent = aircraft.callsign ?? aircraft.icao24.toUpperCase();
  note.textContent = entry.stale
    ? "Stale observation. This is a last-known position."
    : position?.projected
      ? "Surface projection. The marker does not show a measured height."
      : "Position shown at geometric height above the WGS84 ellipsoid.";
  if (!position)
    note.textContent =
      "Position unavailable. Metadata is retained without a globe marker.";
  if (position && !altitude.checked)
    note.textContent =
      "Surface projection selected. Observed altitudes are unchanged.";
  if (position && entry.stale)
    note.textContent = "Stale observation. Motion is stopped.";
  if (historyMode)
    note.textContent = `Historical replay at ${utcLabel(replay.currentTime)}. ${entry.display.reason}.`;
  locate.disabled = !position || !globe;
  follow.disabled = !position || !globe;
  const fields: [string, string][] = [
    ["ICAO24", aircraft.icao24],
    ["Aircraft type", aircraft.aircraft_type ?? "Unknown"],
    ["Symbol", SYMBOL_NAMES[symbolKind(aircraft.aircraft_type)]],
    ["Symbol direction", symbolDirection(aircraft).label],
    [
      historyMode ? "Previous observation" : "Latest position",
      observed
        ? `${observed.latitude.toFixed(5)}°, ${observed.longitude.toFixed(5)}°`
        : "Unknown",
    ],
    [
      "Display position",
      position
        ? `${position.latitude.toFixed(5)}°, ${position.longitude.toFixed(5)}°`
        : "Not displayed",
    ],
    ["Motion", entry.display.reason],
    ["Timeline", historyMode ? "Historical replay" : "Live cache"],
    [
      "Display time",
      entry.display.timestamp === null
        ? "Unknown"
        : new Date(entry.display.timestamp)
            .toISOString()
            .replace("T", " ")
            .replace("Z", " UTC"),
    ],
    [
      "Display altitude",
      position
        ? position.projected
          ? "Surface projection"
          : "Geometric height"
        : "Not displayed",
    ],
    [
      "Display height",
      !position
        ? "Not displayed"
        : position.projected
          ? "Surface projection"
          : `${heightLabel(position.height, selectedUnits)} WGS84${entry.display.mode === "interpolated" ? " (interpolated)" : ""}`,
    ],
    [
      "Observed geometric",
      `${heightLabel(aircraft.geometric_altitude_m, selectedUnits)} · WGS84`,
    ],
    [
      "Barometric",
      `${heightLabel(aircraft.barometric_altitude_m, selectedUnits)} · pressure altitude`,
    ],
    ["Groundspeed", speedLabel(aircraft.groundspeed_mps, selectedUnits)],
    ["Ground track", quantity(aircraft.ground_track_deg, "° true", 1)],
    ["True heading", quantity(aircraft.true_heading_deg, "° reported", 1)],
    [
      "Observation age",
      aircraft.position_observed_at
        ? `${Math.floor(entry.ageSeconds)} seconds`
        : "Unknown",
    ],
    [
      "Observation time",
      aircraft.position_observed_at?.replace("T", " ").replace("Z", " UTC") ??
        "Unknown",
    ],
    ["Reception", aircraft.source_type],
  ];
  detail.replaceChildren(
    ...fields.map(([label, value]) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value;
      row.append(term, description);
      return row;
    }),
  );
}
function renderRoster(): void {
  const shown = planes.slice(0, 100);
  const signature = shown
    .map(
      ({ observation: a, stale, display }) =>
        `${a.icao24}|${a.callsign}|${a.aircraft_type}|${a.barometric_altitude_m}|${units.value}|${stale}|${display.position?.projected}|${historyMode ? "historical" : routeLabel(fleetRoutes.get(a.icao24, a.callsign))}`,
    )
    .join(";");
  if (signature !== previousRoster) {
    previousRoster = signature;
    const focusedId = (document.activeElement as HTMLElement | null)?.dataset
      .icao;
    roster.replaceChildren(
      ...shown.map((entry) => {
        const aircraft = entry.observation;
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.icao = aircraft.icao24;
        button.setAttribute(
          "aria-pressed",
          String(selected === aircraft.icao24),
        );
        const name = document.createElement("span");
        name.textContent = aircraft.callsign ?? aircraft.icao24.toUpperCase();
        const id = document.createElement("small");
        id.textContent = `${aircraft.icao24} · ${aircraft.aircraft_type ?? "type unknown"}`;
        name.append(id);
        const endpoints = document.createElement("small");
        endpoints.className = "roster-route";
        endpoints.textContent = historyMode
          ? "Historical route not recorded"
          : routeLabel(fleetRoutes.get(aircraft.icao24, aircraft.callsign));
        name.append(endpoints);
        const symbol = document.createElement("img");
        const kind = symbolKind(aircraft.aircraft_type);
        symbol.src = symbolImage(kind, false, entry.stale);
        symbol.alt = "";
        symbol.title = SYMBOL_NAMES[kind];
        symbol.className = "roster-symbol";
        symbol.width = symbol.height = 26;
        const status = document.createElement("span");
        status.className = "row-status";
        const position = entry.display.position;
        status.textContent = entry.stale
          ? "Stale"
          : !position
            ? "No position"
            : position.projected
              ? "Projected"
              : heightLabel(
                  aircraft.barometric_altitude_m,
                  units.value as DisplayPreferences["units"],
                );
        button.append(symbol, name, status);
        button.onclick = () => selectAircraft(aircraft.icao24);
        item.append(button);
        return item;
      }),
    );
    if (focusedId)
      roster
        .querySelector<HTMLButtonElement>(`button[data-icao="${focusedId}"]`)
        ?.focus({ preventScroll: true });
  }
  element("#list-count").textContent = `${planes.length} / ${totalAircraft}`;
  element("#list-note").textContent =
    planes.length > 100
      ? "First 100 matches listed. All matching positions remain selectable on the globe."
      : planes.length === 0
        ? historyMode
          ? replay.response?.observations.length
            ? "No historical position is available at this replay time."
            : "History contains no observations for this aircraft and range."
          : totalAircraft
            ? "No aircraft match these filters."
            : state.snapshot?.data.status === "healthy"
              ? "No aircraft reported in this area. The shared feed will update automatically."
              : "Waiting for fresh aircraft observations. Connection details are in the feed menu above."
        : "Select an aircraft to take a closer look.";
}
function updateScene(now: number): { positioned: number; stale: number } {
  const all = historyMode
    ? replay.current(altitude.checked)
    : state.current(now, {
        interpolate: interpolation.checked,
        showAltitude: altitude.checked,
      });
  totalAircraft = all.length;
  // Historical mode is already scoped to one requested aircraft. Live search
  // and freshness filters must not make a loaded replay disappear.
  planes = historyMode
    ? all
    : filterAircraft(all, {
        query: query.value,
        freshOnly: freshOnly.checked,
      });
  if (
    selected &&
    !planes.some((plane) => plane.observation.icao24 === selected)
  )
    selectAircraft(null);
  globe?.setOptions({
    labels: labels.value as GlobeOptions["labels"],
    tracks: tracks.checked,
  });
  const positioned =
    globe?.update(planes) ??
    planes.filter((plane) => plane.display.position).length;
  return { positioned, stale: all.filter((plane) => plane.stale).length };
}
function animate(now: number): void {
  animationFrame = null;
  if (disposed || pageHidden || document.hidden) return;
  // Limit entity updates to 20 Hz; DOM metadata remains on the 1 Hz timer.
  if (now - lastFrame >= 50) {
    const elapsed = lastFrame === 0 ? 0 : now - lastFrame;
    lastFrame = now;
    if (historyMode && historyPlaying) {
      replay.advance(elapsed);
      if (replay.currentTime >= replay.timeline.end) historyPlaying = false;
    }
    updateScene(now);
  }
  syncAnimation();
}
function syncAnimation(): void {
  const active =
    globe &&
    !pageHidden &&
    !document.hidden &&
    !disposed &&
    ((historyMode && historyPlaying) ||
      (!historyMode &&
        interpolation.checked &&
        planes.some((plane) => plane.display.mode === "interpolated")));
  if (active && animationFrame === null)
    animationFrame = requestAnimationFrame(animate);
  else if (!active && animationFrame !== null) {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
  }
}
function render(): void {
  const snapshot = state.snapshot;
  if (disposed) return;
  renderFeedNotice();
  if (!snapshot) return;
  renderCoverage(snapshot);
  element("#route-coverage").textContent = historyMode
    ? "Live references are not applied to replay."
    : fleetRoutes.summary(snapshot);
  const { positioned, stale } = updateScene(performance.now());
  element("#aircraft-count").textContent = globe ? String(positioned) : "—";
  if (historyMode && replay.response) {
    element("#snapshot-status").textContent =
      `${totalAircraft ? "1" : "0"} historical aircraft · ${replay.response.observations.length} observations${replay.response.truncated ? " · result truncated" : ""}`;
    element("#snapshot-age").textContent =
      `Historical clock ${utcLabel(replay.currentTime)}`;
    element("#motion-status").textContent =
      `Historical replay · ${planes.filter((plane) => plane.display.mode === "interpolated").length ? "interpolating" : "holding or observed"}`;
  } else {
    element("#snapshot-status").textContent =
      `${totalAircraft} cached · ${stale} stale · ${snapshot.tracking.poll_interval_seconds}s minimum poll interval${snapshot.data.truncated_aircraft_count ? ` · ${snapshot.data.truncated_aircraft_count} observations omitted by capacity limits (overlaps may repeat)` : ""}`;
    const age = snapshot.data.last_success_at
      ? Math.max(
          0,
          (Date.parse(snapshot.data.emitted_at) -
            Date.parse(snapshot.data.last_success_at)) /
            1000 +
            state.elapsedSeconds(performance.now()),
        )
      : null;
    element("#snapshot-age").textContent =
      age === null
        ? "No successful fetch yet."
        : `Last successful fetch ${Math.floor(age)}s ago.`;
    element("#motion-status").textContent = interpolation.checked
      ? `Delayed display · ${state.delaySeconds}s behind · ${planes.filter((plane) => plane.display.mode === "interpolated").length} interpolating`
      : "Latest observations · motion interpolation off";
  }
  renderRoster();
  renderSelection();
  renderHistoryControls();
  syncAnimation();
}
function accept(snapshot: Snapshot): void {
  if (disposed) return;
  if (!state.apply(snapshot, performance.now())) return;
  globe?.setArea(snapshot.tracking.area);
  globe?.setCoverage(snapshot.data.coverage ?? []);
  void fleetRoutes.update(snapshot);
  const area = snapshot.tracking.area;
  const isDenver =
    Math.abs(area.latitude_deg - 39.8561) < 0.01 &&
    Math.abs(area.longitude_deg + 104.6737) < 0.01;
  const multiRegion = snapshot.tracking.coverage_kind === "sampled_hubs";
  element("#area-name").textContent = multiRegion
    ? "Across six regions"
    : isDenver
      ? "Denver / DEN"
      : "Tracking area";
  element("#area-description").textContent = multiRegion
    ? "Denver · London · Tokyo · Sydney · Dubai · São Paulo"
    : `${area.latitude_deg.toFixed(4)}°, ${area.longitude_deg.toFixed(4)}° · ${area.radius_nm} NM`;
  element("#coverage-area").textContent = multiRegion
    ? "Six sampled hubs · worldwide coverage unavailable"
    : `${area.radius_nm} NM around ${isDenver ? "Denver" : "tracking center"}`;
  if (!historyMode) renderLiveSource(snapshot);
  render();
}
function renderCoverage(snapshot: Snapshot): void {
  const regions = snapshot.data.coverage ?? [];
  const signature = regions.map((region) => region.id).join(",");
  if (coverageView.dataset.regions !== signature) {
    const previous = coverageView.value;
    coverageView.dataset.regions = signature;
    coverageView.replaceChildren(
      ...regions.map((region) => {
        const option = document.createElement("option");
        option.value = region.id;
        option.textContent = region.name;
        return option;
      }),
    );
    if (regions.some((region) => region.id === previous))
      coverageView.value = previous;
  }
  coverageView.disabled = !regions.length || !globe;
  element("#coverage-policy").textContent =
    regions.length > 1
      ? `One shared request every ${snapshot.tracking.poll_interval_seconds}s; each hub approximately every ${snapshot.tracking.coverage_cycle_seconds}s before source delays. These circles do not cover the whole Earth.`
      : "Only the configured circle is tracked. Moving the camera does not change the feed.";
  const now =
    Date.parse(snapshot.data.emitted_at) +
    state.elapsedSeconds(performance.now()) * 1000;
  const rows = regions.map((region) => {
    const item = document.createElement("li");
    const age = region.last_success_at
      ? Math.max(0, (now - Date.parse(region.last_success_at)) / 1000)
      : null;
    const status =
      region.status === "idle"
        ? "awaiting first sample"
        : region.status === "degraded"
          ? "source delayed"
          : age !== null && age >= snapshot.tracking.stale_after_seconds
            ? "stale"
            : "observed";
    item.textContent = `${region.id.toUpperCase()} · ${region.area.radius_nm} NM · ${status}${age === null ? "" : ` · ${Math.floor(age)}s ago`}`;
    if (region.last_error) item.title = region.last_error;
    return item;
  });
  element("#coverage-regions").replaceChildren(...rows);
}
coverageView.onchange = () => {
  const region = state.snapshot?.data.coverage?.find(
    (item) => item.id === coverageView.value,
  );
  if (region) globe?.visitRegion(region.area);
};
follow.onclick = () => {
  globe?.follow(globe.following ? null : selected);
  renderSelection();
};
viewReference.onclick = () => globe?.viewReference();
const wsUrl = new URL("/api/ws", location.href);
wsUrl.protocol = location.protocol === "https:" ? "wss:" : "ws:";
const stream = new SnapshotStream(wsUrl.href, accept, (status) => {
  connection.textContent = status;
  connection.dataset.connected = String(status === "Connected");
  renderFeedNotice();
});
element<HTMLButtonElement>("#reconnect").onclick = () => {
  stream.stop();
  stream.start();
};
element<HTMLButtonElement>("#home").onclick = () => globe?.home();
element<HTMLButtonElement>("#world").onclick = () => globe?.world();
element<HTMLButtonElement>("#airport-view").onclick = () => globe?.airport();
element<HTMLButtonElement>("#terminal-view").onclick = () => globe?.terminal();
element<HTMLButtonElement>("#zoom-in").onclick = () => globe?.zoom(true);
element<HTMLButtonElement>("#zoom-out").onclick = () => globe?.zoom(false);
element<HTMLButtonElement>("#north-up").onclick = () => globe?.topDown();
element<HTMLButtonElement>("#skyline-view").onclick = () => globe?.skyline();
element<HTMLButtonElement>("#civic-view").onclick = () => globe?.civicCenter();
element<HTMLButtonElement>("#terrain-view").onclick = () =>
  globe?.terrainView();
function updateMap(retry = false): void {
  mapPreferences.imagery =
    mapImagery.value === "offline"
      ? "offline"
      : mapImagery.value === "earth"
        ? "earth"
        : "aerial";
  mapPreferences.terrain = mapTerrain.checked;
  mapPreferences.airport = mapAirport.checked;
  mapPreferences.places = mapPlaces.checked;
  mapPreferences.landmarks = mapLandmarks.checked;
  mapTerrain.disabled = mapPreferences.imagery === "offline";
  saveMapPreferences(mapPreferences);
  element("#map-status").textContent = "Loading map layers…";
  void globe
    ?.configureMap(mapPreferences, retry)
    .catch(() =>
      globeFailure(
        "Base map unavailable. Reload to retry; aircraft data remains in the list.",
      ),
    );
}
for (const control of [
  mapImagery,
  mapTerrain,
  mapAirport,
  mapPlaces,
  mapLandmarks,
])
  control.addEventListener("change", () => updateMap());
element<HTMLButtonElement>("#retry-maps").onclick = () => updateMap(true);
for (const control of [interpolation, altitude, tracks, labels, units])
  control.addEventListener("change", () => {
    saveDisplayPreferences({
      units: units.value as DisplayPreferences["units"],
      labels: labels.value as DisplayPreferences["labels"],
      interpolate: interpolation.checked,
      altitude: altitude.checked,
      tracks: tracks.checked,
    });
    render();
  });
freshOnly.addEventListener("change", render);
query.addEventListener("input", render);
setDefaultHistoryRange();
function returnToLive(): void {
  historyRequest++;
  historyAbort?.abort();
  historyMode = false;
  historyPlaying = false;
  replay.clear();
  historyPlayback.hidden = true;
  query.disabled = freshOnly.disabled = interpolation.disabled = false;
  if (state.snapshot) {
    renderLiveSource(state.snapshot);
    globe?.setArea(state.snapshot.tracking.area);
  }
  setHistoryStatus("Live view restored. History is optional local storage.");
  render();
}
historyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const request = ++historyRequest;
  historyAbort?.abort();
  historyAbort = new AbortController();
  setHistoryStatus("Loading recorded observations…");
  try {
    const range = normalizeHistoryRange(historyStart.value, historyEnd.value);
    const result = await fetchHistory(
      historyIcao.value.trim().toLowerCase(),
      historySource.value === "adsblol" ? "adsblol" : "synthetic",
      range.start,
      range.end,
      fetch,
      historyAbort.signal,
    );
    if (request !== historyRequest || disposed) return;
    if (!result.observations.length) {
      returnToLive();
      setHistoryStatus(
        "No recorded positions for this aircraft and UTC range.",
      );
      return;
    }
    replay.load(result);
    historyMode = true;
    historyPlaying = false;
    query.disabled = freshOnly.disabled = interpolation.disabled = true;
    renderHistoricalSource(result);
    setHistoryStatus(
      result.truncated
        ? "Result limited to 5,000 observations. Narrow the UTC range for a complete trajectory."
        : `${result.observations.length} recorded observations. Use Play or the slider; live data continues in the background.`,
    );
    render();
    selectAircraft(result.icao24);
    globe?.select(result.icao24, true);
  } catch (error) {
    if (request !== historyRequest || disposed) return;
    const message =
      error instanceof Error ? error.message : "History request failed.";
    setHistoryStatus(
      message.includes("503")
        ? "History unavailable. Enable local storage and check the database connection."
        : message,
      true,
    );
  }
});
historyLive.onclick = returnToLive;
historyPlay.onclick = () => {
  historyPlaying = !historyPlaying;
  lastFrame = performance.now();
  render();
};
historySlider.addEventListener("input", () => {
  historyPlaying = false;
  replay.setTime(Number(historySlider.value));
  render();
});
function motionPreferenceChanged(): void {
  if (reducedMotion.matches) {
    interpolation.checked = false;
    historyPlaying = false;
    render();
  }
}
reducedMotion.addEventListener("change", motionPreferenceChanged);
function visibilityChanged(): void {
  if (document.hidden) {
    historyPlaying = false;
    syncAnimation();
  } else render();
}
document.addEventListener("visibilitychange", visibilityChanged);
function closeSelection(): void {
  const id = selected;
  selectAircraft(null);
  if (id && !element("#panel-traffic").hidden)
    roster
      .querySelector<HTMLButtonElement>(`button[data-icao="${id}"]`)
      ?.focus({ preventScroll: true });
}
clear.onclick = closeSelection;
element<HTMLButtonElement>("#selection-history").onclick = () => {
  showPanel("replay", true);
  if (matchMedia("(max-width: 760px)").matches)
    element(".explorer").scrollIntoView({
      block: "start",
      behavior: "instant",
    });
};
function keyboardShortcut(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null;
  if (
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    target?.closest("input, select, textarea, [contenteditable=true]")
  )
    return;
  if (event.key === "/") {
    event.preventDefault();
    showPanel("traffic");
    query.focus();
  } else if (event.key === "Escape" && selected) closeSelection();
}
document.addEventListener("keydown", keyboardShortcut);
void checkRecording();
locate.onclick = () => globe?.select(selected, true);
void fetchSnapshot()
  .then(accept)
  .catch(() => {
    if (!disposed)
      connection.textContent = "Backend unavailable · reconnecting";
  })
  .finally(() => {
    if (!disposed && !pageHidden) stream.start();
  });
const refresh = setInterval(render, 1000);
function dispose(): void {
  if (disposed) return;
  disposed = true;
  weather.destroy();
  route.destroy();
  fleetRoutes.destroy();
  historyRequest++;
  historyAbort?.abort();
  availabilityAbort?.abort();
  document.removeEventListener("keydown", keyboardShortcut);
  clearInterval(refresh);
  if (animationFrame !== null) cancelAnimationFrame(animationFrame);
  reducedMotion.removeEventListener("change", motionPreferenceChanged);
  document.removeEventListener("visibilitychange", visibilityChanged);
  stream.stop();
  globe?.destroy();
}
window.addEventListener("pagehide", (event) => {
  pageHidden = true;
  historyPlaying = false;
  syncAnimation();
  if (event.persisted) stream.stop();
  else dispose();
});
window.addEventListener("pageshow", (event) => {
  pageHidden = false;
  if (event.persisted && !disposed) {
    stream.start();
    render();
  }
});
if (import.meta.hot) import.meta.hot.dispose(dispose);
