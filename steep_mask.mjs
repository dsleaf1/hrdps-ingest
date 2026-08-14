#!/usr/bin/env node
/* steep_mask.mjs — fetch the SSCOFS regulargrid land/sea MASK once and cache it.
   ---------------------------------------------------------------------------------------------
   WHY THIS EXISTS, SEPARATELY FROM steep_bake.mjs.
   The 0.56 km steepness bake needs a water mask at the SSCOFS regulargrid's native 0.005°
   spacing for two different jobs:
     1. WHICH FINE CELLS EXIST. Inside the three `sscofs/field` tiles the tile's own 255-nodata
        answers that, but the STEEP_ZONES reach well outside those three tiles (Broughtons,
        Johnstone, E Juan de Fuca), and there is no other 0.56 km water/land information there.
     2. THE DIRECTIONAL FETCH MARCH. The 16-sector ray march walks *outward* up to 60 km, so it
        needs land information well BEYOND each zone. digest #133 marched on a mask that stopped
        at the tile bbox, so rays leaving it capped at 60 km instead of hitting real land — an
        edge effect it recorded as "small but UNQUANTIFIED". Padding the mask by the full 60 km
        cap removes that effect instead of bounding it.
   The mask is BATHYMETRY, not forecast: it is identical in every cycle, so this runs once and the
   result is cached to R2. steep_bake.mjs downloads it and never calls OPeNDAP.

   OUTPUT (./steep_out/, uploaded by steep_upload.py to sscofs/steep/static/):
     mask.json  {lat0,lon0,dx,iy0,ix0,rows,cols}  — iy/ix are indices into the FULL 1553×1519 grid
     mask.bin   rows*cols bytes, 1 = water, 0 = land. Row 0 = SOUTH (grid order, iy grows north).
   ⚠ Row 0 = south here, unlike the field tiles (row 0 = north). The bake converts; keeping grid
   order means an index into this file is a plain (iy-iy0, ix-ix0), with no flip to get wrong.

   Usage: node steep_mask.mjs [--band 50] [--out steep_out]
*/
import { writeFileSync, mkdirSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";

const OPENDAP = "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS";
const LAT0 = 44.37, LON0 = -129.53, DX = 0.005;      // sscofs.py's grid origin, verified in the DDS
const NY = 1553, NX = 1519;                          // regulargrid dims (DDS, 2026-08-13)

/* The window: the union of STEEP_ZONES padded by the fetch cap, clipped to the grid.
   ⚠ keep STEEP_ZONES in sync with unified_map.html:521 and steep_bake.mjs. */
const STEEP_ZONES = [
  [-123.30, 47.00, -122.20, 48.35], [-123.65, 48.05, -122.70, 48.55],
  [-123.95, 48.30, -122.50, 49.30], [-125.25, 48.90, -122.90, 50.15],
  [-127.20, 49.90, -124.20, 51.05], [-128.15, 50.35, -125.80, 51.10],
];
const FETCH_CAP_KM = 60;

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const BAND = +arg("--band", 50);                     // rows per OPeNDAP request
const OUT = arg("--out", "steep_out");
const log = m => process.stderr.write(m + "\n");

function curl(url, tmo = 180) {
  for (let a = 0; a < 3; a++) {
    try {
      const t = execFileSync("curl", ["-sg", "--max-time", String(tmo), url],
        { encoding: "utf8", maxBuffer: 1 << 28 });
      if (t && !t.slice(0, 200).includes("Error {") && !t.slice(0, 200).includes("HTTP Status")) return t;
    } catch (e) { /* retry */ }
    log(`  curl retry ${a + 1}`);
  }
  throw new Error("curl failed after 3 attempts: " + url);
}

/* Any cycle will do — the mask is static. Walk back through today/yesterday and the four cycles
   until a .dds answers, so this never depends on a particular run being posted. */
function anyCycle() {
  for (let back = 0; back < 3; back++) {
    const d = new Date(Date.now() - back * 864e5);
    const day = d.toISOString().slice(0, 10).replace(/-/g, "/"), ymd = day.replace(/\//g, "");
    for (const cyc of ["t21z", "t15z", "t09z", "t03z"]) {
      const u = `${OPENDAP}/${day}/sscofs.${cyc}.${ymd}.regulargrid.f000.nc.dds`;
      let t = ""; try { t = execFileSync("curl", ["-sg", "--max-time", "30", u], { encoding: "utf8" }); } catch (_) {}
      if (t.includes("mask[ny")) return { day, cyc, ymd };
    }
  }
  throw new Error("no SSCOFS regulargrid cycle answered");
}

/* Rows of a THREDDS 2-D hyperslab look like "[r], v0, v1, …" with r DENSE inside the slab. */
function parseRows(txt, va, cols) {
  const m = txt.match(new RegExp(va + "\\[\\d[^\\n]*\\n([\\s\\S]*?)(?=\\n[A-Za-z_]+\\[\\d|$)"));
  if (!m) throw new Error(`no ${va} block in the response`);
  const out = new Map();
  for (const row of m[1].matchAll(/\[(\d+)\],\s*([-\d.eE, ]+)/g)) {
    const vals = row[2].split(",").map(s => s.trim()).filter(s => s !== "");
    if (vals.length !== cols) throw new Error(`row ${row[1]}: ${vals.length} values, expected ${cols}`);
    out.set(+row[1], vals);
  }
  return out;
}

const W = Math.min(...STEEP_ZONES.map(z => z[0])), S = Math.min(...STEEP_ZONES.map(z => z[1]));
const E = Math.max(...STEEP_ZONES.map(z => z[2])), N = Math.max(...STEEP_ZONES.map(z => z[3]));
const padLat = FETCH_CAP_KM / 111.32;
const padLon = FETCH_CAP_KM / (111.32 * Math.cos((N + S) / 2 * Math.PI / 180));
const iy0 = Math.max(0, Math.round((S - padLat - LAT0) / DX)), iy1 = Math.min(NY - 1, Math.round((N + padLat - LAT0) / DX));
const ix0 = Math.max(0, Math.round((W - padLon - LON0) / DX)), ix1 = Math.min(NX - 1, Math.round((E + padLon - LON0) / DX));
const rows = iy1 - iy0 + 1, cols = ix1 - ix0 + 1;

const cyc = anyCycle();
log(`cycle ${cyc.day} ${cyc.cyc} (mask is static — the cycle is irrelevant beyond availability)`);
log(`window iy ${iy0}..${iy1} (${rows} rows, ${(LAT0 + iy0 * DX).toFixed(3)}–${(LAT0 + iy1 * DX).toFixed(3)} N) ` +
    `· ix ${ix0}..${ix1} (${cols} cols, ${(LON0 + ix0 * DX).toFixed(3)}–${(LON0 + ix1 * DX).toFixed(3)} E)`);
log(`${(rows * cols / 1e6).toFixed(2)} M cells in ${Math.ceil(rows / BAND)} requests`);

const mask = new Uint8Array(rows * cols);
let water = 0;
for (let r0 = 0; r0 < rows; r0 += BAND) {
  const a = iy0 + r0, b = Math.min(iy1, a + BAND - 1);
  const q = encodeURIComponent(`mask[${a}:${b}][${ix0}:${ix1}]`).replace(/%2C/g, ",");
  const txt = curl(`${OPENDAP}/${cyc.day}/sscofs.${cyc.cyc}.${cyc.ymd}.regulargrid.f000.nc.ascii?` + q);
  const got = parseRows(txt, "mask", cols);
  if (got.size !== b - a + 1) throw new Error(`band ${a}..${b}: ${got.size} rows, expected ${b - a + 1}`);
  for (const [rr, vals] of got) {
    const base = (r0 + rr) * cols;
    for (let c = 0; c < cols; c++) { const w = +vals[c] === 1 ? 1 : 0; mask[base + c] = w; water += w; }
  }
  log(`  rows ${a}..${b}  (${(100 * (r0 + BAND) / rows).toFixed(0)}%)`);
}

if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
writeFileSync(OUT + "/mask.bin", Buffer.from(mask));
writeFileSync(OUT + "/mask.json", JSON.stringify(
  { note: "SSCOFS regulargrid land/sea mask, 1=water. Row 0 = SOUTH. Static across cycles.",
    lat0: LAT0, lon0: LON0, dx: DX, ny: NY, nx: NX, iy0, ix0, rows, cols,
    built: new Date().toISOString(), from_cycle: `${cyc.day} ${cyc.cyc}` }, null, 1));
log(`wrote ${OUT}/mask.bin — ${water.toLocaleString()} water of ${(rows * cols).toLocaleString()} ` +
    `(${(100 * water / (rows * cols)).toFixed(1)}%)`);
