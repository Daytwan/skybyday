import { readFileSync } from "node:fs";
import { parseSnapshot, type Snapshot } from "./snapshot.ts";

// Test helper only; never imported by the application entry point.
export function fixture(): Snapshot {
  return parseSnapshot(
    JSON.parse(
      readFileSync(
        new URL("../../tests/fixtures/snapshot.json", import.meta.url),
        "utf8",
      ),
    ),
  );
}
