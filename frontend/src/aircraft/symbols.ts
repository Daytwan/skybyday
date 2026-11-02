import type { Aircraft } from "../network/snapshot.ts";

export type SymbolKind =
  | "airplane"
  | "airliner"
  | "widebody"
  | "business"
  | "prop"
  | "helicopter";

// A deliberately small exact-code lookup, not a guess from callsigns or prefixes.
// Unlisted ICAO type designators keep the generic airplane symbol.
const TYPES: Record<Exclude<SymbolKind, "airplane">, readonly string[]> = {
  airliner: [
    "A318",
    "A319",
    "A320",
    "A321",
    "A19N",
    "A20N",
    "A21N",
    "BCS1",
    "BCS3",
    "B712",
    "B732",
    "B733",
    "B734",
    "B735",
    "B736",
    "B737",
    "B738",
    "B739",
    "B37M",
    "B38M",
    "B39M",
    "B752",
    "B753",
    "CRJ2",
    "CRJ7",
    "CRJ9",
    "CRJX",
    "E135",
    "E145",
    "E170",
    "E175",
    "E190",
    "E195",
    "E290",
    "E295",
  ],
  widebody: [
    "A306",
    "A310",
    "A332",
    "A333",
    "A338",
    "A339",
    "A342",
    "A343",
    "A345",
    "A346",
    "A359",
    "A35K",
    "A388",
    "B744",
    "B748",
    "B762",
    "B763",
    "B764",
    "B772",
    "B773",
    "B77L",
    "B77W",
    "B788",
    "B789",
    "B78X",
    "MD11",
  ],
  business: [
    "C25A",
    "C25B",
    "C25C",
    "C510",
    "C525",
    "C550",
    "C560",
    "C56X",
    "C650",
    "C680",
    "C68A",
    "C700",
    "C750",
    "CL30",
    "CL35",
    "CL60",
    "GL5T",
    "GL6T",
    "GL7T",
    "GLEX",
    "GLF4",
    "GLF5",
    "GLF6",
    "GA5C",
    "GA6C",
    "FA7X",
    "FA8X",
    "F900",
    "F2TH",
    "LJ35",
    "LJ45",
    "LJ60",
    "PC24",
  ],
  prop: [
    "C150",
    "C152",
    "C162",
    "C172",
    "C177",
    "C182",
    "C206",
    "C208",
    "C210",
    "C310",
    "C414",
    "C421",
    "PA28",
    "P28A",
    "P28B",
    "P28R",
    "P28T",
    "PA44",
    "PA46",
    "SR20",
    "SR22",
    "DA40",
    "DA42",
    "DA62",
    "BE20",
    "BE30",
    "BE36",
    "BE58",
    "PC12",
    "TBM7",
    "TBM8",
    "TBM9",
    "AT43",
    "AT45",
    "AT72",
    "AT75",
    "AT76",
    "DH8A",
    "DH8B",
    "DH8C",
    "DH8D",
  ],
  helicopter: [
    "R22",
    "R44",
    "R66",
    "B06",
    "B407",
    "B412",
    "B429",
    "EC35",
    "EC45",
    "EC55",
    "AS50",
    "AS55",
    "AS65",
    "A109",
    "A119",
    "A139",
    "S76",
    "S92",
    "H60",
  ],
};

export function symbolKind(type: string | null): SymbolKind {
  const code = type?.trim().toUpperCase();
  if (!code) return "airplane";
  for (const [kind, codes] of Object.entries(TYPES)) {
    if (codes.includes(code)) return kind as SymbolKind;
  }
  return "airplane";
}

export const SYMBOL_NAMES: Record<SymbolKind, string> = {
  airplane: "Generic aircraft",
  airliner: "Airliner / regional jet",
  widebody: "Widebody jet",
  business: "Business jet",
  prop: "Propeller aircraft",
  helicopter: "Helicopter",
};

export function symbolDirection(
  aircraft: Pick<Aircraft, "true_heading_deg" | "ground_track_deg">,
): { degrees: number | null; label: string } {
  if (aircraft.true_heading_deg !== null)
    return {
      degrees: aircraft.true_heading_deg,
      label: "Reported true heading",
    };
  if (aircraft.ground_track_deg !== null)
    return {
      degrees: aircraft.ground_track_deg,
      label: "Reported ground track (not heading)",
    };
  return {
    degrees: null,
    label: "Unknown · symbol has no geographic orientation",
  };
}

// Original SkybyDay vector artwork. Nose points up; neither pitch nor roll is inferred.
const PATHS: Record<SymbolKind, string> = {
  airplane:
    "M32 5 36 10 36 26 55 39 55 44 36 37 36 51 43 56 43 60 32 56 21 60 21 56 28 51 28 37 9 44 9 39 28 26 28 10Z",
  airliner:
    "M32 3C35 3 36 10 36 14L36 27 57 40 57 45 36 38 36 51 44 56 44 60 32 56 20 60 20 56 28 51 28 38 7 45 7 40 28 27 28 14C28 10 29 3 32 3Z M18 30H23V39H18Z M41 30H46V39H41Z",
  widebody:
    "M32 2C36 2 38 10 38 17L38 29 60 39 60 45 38 39 37 51 46 56 46 61 32 57 18 61 18 56 27 51 26 39 4 45 4 39 26 29 26 17C26 10 28 2 32 2Z M12 31H17V41H12Z M21 27H26V38H21Z M38 27H43V38H38Z M47 31H52V41H47Z",
  business:
    "M32 4 35 11 35 28 53 43 53 47 35 39 35 52 44 57 44 60 32 57 20 60 20 57 29 52 29 39 11 47 11 43 29 28 29 11Z M23 41H28V51H23Z M36 41H41V51H36Z",
  prop: "M30 5H34V12H43V15H34L36 27 57 28 57 34 36 35 35 52 43 54 43 59 32 57 21 59 21 54 29 52 28 35 7 34 7 28 28 27 30 15H21V12H30Z",
  helicopter:
    "M32 10C39 10 40 20 38 30L35 37 34 54 41 54 41 58 23 58 23 54 30 54 29 37 26 30C24 20 25 10 32 10Z M6 22H58V26H6Z M30 3H34V46H30Z M21 15H23V39H21Z M41 15H43V39H41Z",
};
const images = new Map<string, string>();

export function symbolImage(
  kind: SymbolKind,
  projected = false,
  stale = false,
  unoriented = false,
): string {
  const key = `${kind}/${projected}/${stale}/${unoriented}`;
  const cached = images.get(key);
  if (cached) return cached;
  const color = stale ? "#a6b3bb" : "#b4f7e3";
  const path = PATHS[kind];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">${unoriented ? '<circle cx="32" cy="32" r="30" fill="#10191f" fill-opacity=".75" stroke="#a6b3bb" stroke-width="2"/>' : ""}<g${unoriented ? ' transform="translate(10 10) scale(.68)"' : ""}><path d="${path}" fill="none" stroke="#10191f" stroke-width="5" stroke-linejoin="round"/><path d="${path}" fill="${projected ? "#17272d" : color}" stroke="${color}" stroke-width="${projected ? 2 : 1}" stroke-linejoin="round"/></g></svg>`;
  const image = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  images.set(key, image);
  return image;
}
