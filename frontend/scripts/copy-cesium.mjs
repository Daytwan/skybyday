import { cp, mkdir } from "node:fs/promises";

const root = new URL("../", import.meta.url);
const packageRoot = new URL("node_modules/cesium/", root);
const output = new URL("public/cesium/", root);
await mkdir(output, { recursive: true });
for (const folder of ["Assets", "Workers", "Widgets", "ThirdParty"]) {
  await cp(
    new URL(`Build/Cesium/${folder}`, packageRoot),
    new URL(folder, output),
    { recursive: true },
  );
}
for (const file of ["LICENSE.md", "ThirdParty.json", "ThirdParty.extra.json"]) {
  await cp(new URL(file, packageRoot), new URL(file, output));
}
console.log(
  "Cesium assets copied locally. No map token or tile service required.",
);
