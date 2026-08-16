#!/usr/bin/env node
/* divergence_scan.mjs — nightly model-divergence scanner (Model_Replay_Plan.md step 3).
   =======================================================================================
   For each archived day, run physics.js under Node over every overview-grid water cell ×
   hour × 4 model combos (ray/dir × mono/spec). Emit:

     archive/divergence/<YYYYMMDD>.json — per cell-hour: band per combo, spread, direction
     archive/events.json — top 50 ranked divergence clusters (under-warning above equal-score)

   The overview grid (~2.2 km) is deliberately COARSER than the 0.56 km bake: this scanner
   compares models against each other, not against reality. The coarse grid keeps the nightly
   run under a minute and matches the steepness layer's own zoom-out resolution.

   Reads from R2:
     archive/current_field/<YYYYMMDD>.bin + .json  (stitched best-lead overview current)
     archive/wind/<region>/<YYYYMMDD>.bin + .json   (stitched best-lead 2.5 km wind)
     archive/index.json                             (which days are available)
     sscofs/field/manifest.json                     (overview grid geometry)
     hrdps/manifest.json                            (wind grid geometry)

   Writes to R2 (via the upload step — this script writes local files):
     archive/divergence/<YYYYMMDD>.json
     archive/events.json

   Usage:
     node divergence_scan.mjs --date 20260801                    # scan one day
     node divergence_scan.mjs --date 20260801 --end 20260810     # scan a range
     node divergence_scan.mjs --new                              # scan days not yet scanned
     node divergence_scan.mjs --rebuild                          # re-scan everything
     node divergence_scan.mjs --out scan_out                     # output directory (default)

   ⛔ NEVER writes sscofs/field/overview.bin — the coast map reads it.
*/
import { gunzipSync } from "node:zlib";
import { writeFileSync, readFileSync, existsSync, mkdirSync, readdirSync } from "node:fs";
import { createHash } from "node:crypto";

/* ------------------------------------------------------------------ config */
const R2 = "https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev";
const PHYSICS_URL = "https://dsleaf1.github.io/salish-marine-map/physics.js";

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const has = k => process.argv.includes(k);
const OUT = arg("--out", "scan_out");
const PHYSFILE = arg("--physics-file", "");
const DATE = arg("--date", "");
const END = arg("--end", "");
const NEW_ONLY = has("--new");
const REBUILD = has("--rebuild");

const FETCH_SECTORS = 16, FETCH_CAP_KM = 60;
const RING_MIN = 2;

/* The 4 model combos — the replay plan's two axes: sea model × blocking model. */
const COMBOS = [
  { id: "ray_mono", sea: "ray", block: "mono" },
  { id: "ray_spec", sea: "ray", block: "spec" },
  { id: "dir_mono", sea: "dir", block: "mono" },
  { id: "dir_spec", sea: "dir", block: "spec" },
];

/* Wind archive regions (must match ingest.py ARCHIVE_REGIONS). */
const WIND_REGIONS = [
  "broughtons-discovery", "northern-georgia-strait",
  "san-juan-gulf-islands", "puget-sound",
];

/* Steepness zones — the overview cells inside these boxes are what we scan.
   Keep in sync with steep_bake.mjs / unified_map.html:521. */
const STEEP_ZONES = [
  ["puget",      [-123.30, 47.00, -122.20, 48.35]],
  ["ejdf",       [-123.65, 48.05, -122.70, 48.55]],
  ["sanjuan",    [-123.95, 48.30, -122.50, 49.30]],
  ["georgia",    [-125.25, 48.90, -122.90, 50.15]],
  ["discovery",  [-127.20, 49.90, -124.20, 51.05]],
  ["johnstone",  [-128.15, 50.35, -125.80, 51.10]],
];

const log = m => process.stderr.write(m + "\n");
const T0 = Date.now();
const el = () => ((Date.now() - T0) / 1000).toFixed(1) + "s";

/* ------------------------------------------------------------------ byte fetch */
async function getBytes(url) {
  const r = await fetch(url); if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  let b = new Uint8Array(await r.arrayBuffer());
  if (b[0] === 0x1f && b[1] === 0x8b) b = new Uint8Array(gunzipSync(b));
  return b;
}
const getJSON = async u => JSON.parse(Buffer.from(await getBytes(u)).toString("utf8"));
async function tryGetJSON(u) {
  try { return await getJSON(u); } catch { return null; }
}
async function tryGetBytes(u) {
  try { return await getBytes(u); } catch { return null; }
}

/* ------------------------------------------------------------------ physics.js */
const physSrc = PHYSFILE
  ? readFileSync(PHYSFILE, "utf8")
  : await fetch(PHYSICS_URL).then(r => { if (!r.ok) throw new Error(`physics.js HTTP ${r.status}`); return r.text(); });
if (!/function\s+compute\s*\(/.test(physSrc)) throw new Error("physics.js does not define compute()");
if (/getElementById|\$\(/.test(physSrc)) throw new Error("physics.js references the DOM");
const modelHash = createHash("sha256").update(physSrc).digest("hex").slice(0, 12);
const physSha1 = createHash("sha1").update(physSrc).digest("hex");
const { steepCell, windSea, hazard } = new Function(physSrc + ";return {steepCell, windSea, hazard};")();
log(`physics.js ${PHYSFILE || PHYSICS_URL} · modelHash ${modelHash} · sha1 ${physSha1.slice(0, 12)}`);

/* steepCell hardcodes the DIRECTIONAL fan (±80°, Ueff<0.3 skip, Hs>0.05 skip). The RAY model
   is a single ray along windFromDeg with NO fan filters — compute() achieves this via span=0.
   scanCell() wraps both sea models, passing the block model through. */
function scanCell(Ums, windFromDeg, curKt, curToward, fetchAt, sea, block) {
  if (sea === "dir") return steepCell(Ums, windFromDeg, curKt, curToward, fetchAt, block);
  const F = fetchAt(windFromDeg) * 1000, w = windSea(Ums, F);
  const dom = { Hs: w.Hs, Tp: w.Tp, from: windFromDeg, Fkm: F / 1000 };
  const h = hazard(w.Hs, w.Tp, windFromDeg, curKt, curToward, block, true);
  return { band: h.band, dom };
}

/* ------------------------------------------------------------------ grid geometry from live manifests */
const [fieldMan, windMan] = await Promise.all([
  getJSON(R2 + "/sscofs/field/manifest.json"),
  getJSON(R2 + "/hrdps/manifest.json"),
]);
const ov = fieldMan.regions.overview;
const oCols = ov.cols, oRows = ov.rows, oSpan = oCols * oRows;
const [oW, oS, oE, oN] = ov.bbox;
const oStepLon = (oE - oW) / (oCols - 1), oStepLat = (oN - oS) / (oRows - 1);

const wc = windMan.regions.overview;
const wCols = wc.cols, wRows = wc.rows, wSpan = wCols * wRows;
const [wW, wS, wE, wN] = wc.bbox;
const wStepLon = (wE - wW) / (wCols - 1), wStepLat = (wN - wS) / (wRows - 1);

log(`overview ${oCols}×${oRows} @ ${(oStepLat * 111.32).toFixed(2)} km · wind ${wCols}×${wRows} @ ${(wStepLat * 111.32).toFixed(2)} km`);

/* ------------------------------------------------------------------ overview water mask from live overview.bin
   We need the water mask (wet-in-any-hour) to compute fetch. Since we're reading archived
   day files that might have different hour coverage, use the LIVE overview.bin for the static
   water mask — bathymetry doesn't change between runs. */
const liveOvBytes = await getBytes(R2 + "/sscofs/field/overview.bin");
const liveOvHours = fieldMan.hours.length;
const ovWet = new Uint8Array(oSpan);
for (let h = 0; h < liveOvHours; h++) {
  const o = h * 2 * oSpan;
  for (let i = 0; i < oSpan; i++) if (liveOvBytes[o + i] !== 255) ovWet[i] = 1;
}
log(`[${el()}] water mask: ${ovWet.reduce((a, v) => a + v, 0)} wet cells of ${oSpan}`);

/* ------------------------------------------------------------------ build scannable cell list
   Only overview cells inside a steepness zone AND wet. */
function inAnyZone(lo, la) {
  for (const [, bb] of STEEP_ZONES) {
    if (lo >= bb[0] && lo <= bb[2] && la >= bb[1] && la <= bb[3]) return true;
  }
  return false;
}

const cells = [];
for (let r = 0; r < oRows; r++) {
  const la = oN - r * oStepLat;
  for (let c = 0; c < oCols; c++) {
    const oi = r * oCols + c;
    if (!ovWet[oi]) continue;
    const lo = oW + c * oStepLon;
    if (!inAnyZone(lo, la)) continue;
    let wj = Math.round((lo - wW) / wStepLon), wi = Math.round((wN - la) / wStepLat);
    wj = Math.min(Math.max(wj, 0), wCols - 1); wi = Math.min(Math.max(wi, 0), wRows - 1);
    cells.push({ oi, r, c, lo, la, wCell: wi * wCols + wj });
  }
}
log(`[${el()}] ${cells.length} overview cells inside steepness zones`);

/* ------------------------------------------------------------------ directional fetch at overview resolution
   Static geometry — march once. 16 sectors, overview grid step, 60 km cap.
   Uses the overview water mask. */
const oStepKm = oStepLat * 111.32;
const MAX_STEPS = Math.ceil(FETCH_CAP_KM / oStepKm);
const SEC_STEP = 360 / FETCH_SECTORS;

const fetchKm = new Float32Array(cells.length * FETCH_SECTORS);
log(`[${el()}] marching fetch (${cells.length} cells × ${FETCH_SECTORS} sectors)…`);
for (let s = 0; s < FETCH_SECTORS; s++) {
  const b = s * 2 * Math.PI / FETCH_SECTORS, dN = Math.cos(b), dE = Math.sin(b);
  for (let m = 0; m < cells.length; m++) {
    let la = cells[m].la, lo = cells[m].lo, f = FETCH_CAP_KM;
    for (let k = 1; k <= MAX_STEPS; k++) {
      la += dN * oStepLat;
      lo += dE * oStepLon / Math.cos(la * Math.PI / 180);
      const rr = Math.round((oN - la) / oStepLat), cc = Math.round((lo - oW) / oStepLon);
      if (rr < 0 || rr >= oRows || cc < 0 || cc >= oCols) break;
      if (!ovWet[rr * oCols + cc]) { f = k * oStepKm; break; }
    }
    fetchKm[m * FETCH_SECTORS + s] = f;
  }
}
log(`[${el()}] fetch march done`);

/* ------------------------------------------------------------------ which archived wind region covers each cell */
function windRegionFor(lo, la) {
  for (const reg of WIND_REGIONS) {
    const m = windMan.regions[reg];
    if (!m) continue;
    const [W, S, E, N] = m.bbox;
    if (lo >= W && lo <= E && la >= S && la <= N) return reg;
  }
  return null;
}
const cellWindRegion = cells.map(c => windRegionFor(c.lo, c.la));

/* ------------------------------------------------------------------ determine which dates to scan */
const archiveIndex = await getJSON(R2 + "/archive/index.json");

function availableDates() {
  const windDates = new Set();
  for (const reg of WIND_REGIONS) {
    const days = archiveIndex.wind[reg];
    if (!days) continue;
    for (const d of Object.keys(days)) windDates.add(d);
  }
  const curDates = new Set(Object.keys(archiveIndex.current_field || {}));
  const both = [...windDates].filter(d => curDates.has(d)).sort();
  return both;
}

let datesToScan;
if (DATE) {
  if (END) {
    datesToScan = [];
    let d = DATE;
    while (d <= END) {
      datesToScan.push(d);
      const y = +d.slice(0, 4), m = +d.slice(4, 6) - 1, day = +d.slice(6, 8);
      const next = new Date(Date.UTC(y, m, day + 1));
      d = next.toISOString().slice(0, 10).replace(/-/g, "");
    }
  } else {
    datesToScan = [DATE];
  }
} else {
  const all = availableDates();
  if (NEW_ONLY) {
    const existingDiv = new Set();
    if (existsSync(OUT)) {
      for (const f of readdirSync(OUT)) {
        const m = f.match(/^(\d{8})\.json$/);
        if (m) existingDiv.add(m[1]);
      }
    }
    const existingR2 = new Set(Object.keys(archiveIndex.divergence || {}));
    datesToScan = all.filter(d => !existingDiv.has(d) && !existingR2.has(d));
  } else if (REBUILD) {
    datesToScan = all;
  } else {
    log("specify --date, --new, or --rebuild");
    process.exit(1);
  }
}
log(`[${el()}] ${datesToScan.length} date(s) to scan: ${datesToScan.slice(0, 5).join(", ")}${datesToScan.length > 5 ? "…" : ""}`);
if (!datesToScan.length) { log("nothing to scan"); process.exit(0); }

/* ------------------------------------------------------------------ scan one day */
if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });

/* All divergence results across all days, for the events roll-up at the end. */
const allDivergence = [];

async function scanDay(date) {
  log(`\n── scanning ${date} ──`);

  /* Load archived current field for this day */
  const curSide = await tryGetJSON(R2 + `/archive/current_field/${date}.json`);
  const curBytes = await tryGetBytes(R2 + `/archive/current_field/${date}.bin`);
  if (!curSide || !curBytes) { log(`  ⚠ no archived current field for ${date}, skipping`); return null; }
  if (curSide.cols !== oCols || curSide.rows !== oRows) { log(`  ⚠ current field dims mismatch for ${date}, skipping`); return null; }
  const n = oCols * oRows * 2;
  if (curBytes.length !== 24 * n) { log(`  ⚠ current field size mismatch for ${date} (${curBytes.length} vs ${24 * n}), skipping`); return null; }

  /* Load archived wind for each region that covers our cells */
  const neededRegions = new Set(cellWindRegion.filter(r => r));
  const windData = {};
  for (const reg of neededRegions) {
    const ws = await tryGetJSON(R2 + `/archive/wind/${reg}/${date}.json`);
    const wb = await tryGetBytes(R2 + `/archive/wind/${reg}/${date}.bin`);
    if (!ws || !wb) { log(`  ⚠ no wind for ${reg} on ${date}`); continue; }
    const wn = ws.cols * ws.rows * 2;
    if (wb.length !== 24 * wn) { log(`  ⚠ wind size mismatch for ${reg} on ${date}`); continue; }
    windData[reg] = { meta: ws, bytes: wb, cols: ws.cols, rows: ws.rows, span: ws.cols * ws.rows };
  }

  /* Determine which hours have both current and at least some wind */
  const hours = [];
  for (let h = 0; h < 24; h++) {
    if (!curSide.src[h]) continue;
    let hasWind = false;
    for (const reg of neededRegions) {
      if (windData[reg] && windData[reg].meta.src && windData[reg].meta.src[h]) { hasWind = true; break; }
    }
    if (hasWind) hours.push(h);
  }
  if (!hours.length) { log(`  ⚠ no shared hours for ${date}, skipping`); return null; }
  log(`  ${hours.length} hours with both current + wind`);

  /* Per-cell per-hour: score all 4 combos */
  const dayResults = [];

  for (const h of hours) {
    const curOff = h * n;
    for (let m = 0; m < cells.length; m++) {
      const cell = cells[m];
      const reg = cellWindRegion[m];

      /* Read current at this overview cell */
      const cSpd = curBytes[curOff + cell.oi];
      if (cSpd === 255) continue;
      const curKt = cSpd / 10;
      const curToward = curBytes[curOff + oSpan + cell.oi] * 360 / 255;

      /* Read wind — need the per-region wind grid */
      if (!reg || !windData[reg]) continue;
      const wd = windData[reg];
      const wMeta = wd.meta;
      const [rwW, rwS, rwE, rwN] = wMeta.bbox;
      const rwStepLon = (rwE - rwW) / (wMeta.cols - 1);
      const rwStepLat = (rwN - rwS) / (wMeta.rows - 1);
      let wc2 = Math.round((cell.lo - rwW) / rwStepLon);
      let wr2 = Math.round((rwN - cell.la) / rwStepLat);
      wc2 = Math.min(Math.max(wc2, 0), wMeta.cols - 1);
      wr2 = Math.min(Math.max(wr2, 0), wMeta.rows - 1);
      const wIdx = wr2 * wMeta.cols + wc2;
      const wOff = h * wd.span * 2;
      const wSpd = wd.bytes[wOff + wIdx];
      if (wSpd === 255) continue;
      const U = wSpd / 3.6;   // km/h → m/s
      const dirFrom = wd.bytes[wOff + wd.span + wIdx] * 360 / 255;

      /* Fetch interpolator for this cell */
      const fb = m * FETCH_SECTORS, F = fetchKm;
      const fetchAt = beta => {
        const q = (((beta / SEC_STEP) % FETCH_SECTORS) + FETCH_SECTORS) % FETCH_SECTORS;
        const s0 = q | 0, s1 = (s0 + 1) % FETCH_SECTORS, fr = q - s0;
        return 1 / ((1 - fr) / F[fb + s0] + fr / F[fb + s1]);
      };

      /* Score all 4 combos */
      const bands = {};
      for (const combo of COMBOS) {
        const r = scanCell(U, dirFrom, curKt, curToward, fetchAt, combo.sea, combo.block);
        bands[combo.id] = r.band;
      }

      const vals = Object.values(bands);
      const maxB = Math.max(...vals);
      const minB = Math.min(...vals.filter(v => v >= 0));
      const spread = (minB >= 0) ? maxB - minB : 0;

      if (spread === 0) continue;

      /* Direction flag: does the fixed model (dir_spec) warn more or less than legacy (ray_mono)? */
      let direction = "mixed";
      const fixed = bands.dir_spec, legacy = bands.ray_mono;
      if (fixed > legacy) direction = "under-warn";
      else if (legacy > fixed) direction = "over-warn";

      dayResults.push({
        oi: cell.oi, h, r: cell.r, c: cell.c,
        lo: +cell.lo.toFixed(3), la: +cell.la.toFixed(3),
        bands, spread, direction,
      });
    }
  }

  log(`  ${dayResults.length} divergent cell-hours (spread > 0)`);

  /* Write per-day divergence file */
  const dayDoc = {
    v: 1, date, hours, physics: { modelHash, sha1: physSha1.slice(0, 12) },
    combos: COMBOS.map(c => c.id),
    grid: { cols: oCols, rows: oRows, bbox: [oW, oS, oE, oN], res_deg: +oStepLat.toFixed(4) },
    cells_scanned: cells.length,
    divergent_cell_hours: dayResults.length,
    results: dayResults,
  };
  writeFileSync(`${OUT}/${date}.json`, JSON.stringify(dayDoc, null, 1));
  log(`  wrote ${OUT}/${date}.json`);

  allDivergence.push({ date, results: dayResults });
  return dayResults;
}

/* ------------------------------------------------------------------ run all dates */
for (const date of datesToScan) {
  await scanDay(date);
}

/* ------------------------------------------------------------------ events roll-up
   Cluster contiguous divergent cell-hours and rank by score = spread × cells × hours.
   Under-warning always outranks equal-score (invariant 3 from dev reference). */

function buildEvents() {
  /* Flatten all divergent cell-hours into a list with (date, hour, row, col, spread, direction). */
  const flat = [];
  for (const { date, results } of allDivergence) {
    for (const r of results) {
      flat.push({ date, h: r.h, r: r.r, c: r.c, lo: r.lo, la: r.la,
                  spread: r.spread, direction: r.direction, bands: r.bands });
    }
  }
  if (!flat.length) return [];

  /* Spatial-index clustering: bucket by (date, h÷2, r÷4, c÷4) then BFS within neighbors.
     Adjacency: same date, hours within 1, cells within 3 grid steps — the 4-wide buckets
     guarantee any neighbor lands in the same or an adjacent bucket. */
  const bk = (date, h, r, c) => `${date}:${h >> 1}:${r >> 2}:${c >> 2}`;
  const idx = new Map();
  for (let i = 0; i < flat.length; i++) {
    const f = flat[i], k = bk(f.date, f.h, f.r, f.c);
    let a = idx.get(k); if (!a) idx.set(k, a = []); a.push(i);
  }
  function* neighbors(f) {
    const dh0 = f.h >> 1, dr0 = f.r >> 2, dc0 = f.c >> 2;
    for (let dh = -1; dh <= 1; dh++)
      for (let dr = -1; dr <= 1; dr++)
        for (let dc = -1; dc <= 1; dc++) {
          const a = idx.get(`${f.date}:${dh0 + dh}:${dr0 + dr}:${dc0 + dc}`);
          if (a) yield* a;
        }
  }

  const clusters = [];
  const used = new Set();

  for (let i = 0; i < flat.length; i++) {
    if (used.has(i)) continue;
    const cluster = [flat[i]];
    used.add(i);

    let qi = 0;
    while (qi < cluster.length) {
      const cur = cluster[qi++];
      for (const j of neighbors(cur)) {
        if (used.has(j)) continue;
        const cand = flat[j];
        if (Math.abs(cand.h - cur.h) > 1) continue;
        if (Math.abs(cand.r - cur.r) > 3 || Math.abs(cand.c - cur.c) > 3) continue;
        cluster.push(cand);
        used.add(j);
      }
    }
    clusters.push(cluster);
  }

  /* Score each cluster */
  const events = clusters.map((cl, idx) => {
    const maxSpread = Math.max(...cl.map(c => c.spread));
    const uniqueCells = new Set(cl.map(c => `${c.r},${c.c}`)).size;
    const uniqueHours = new Set(cl.map(c => c.h)).size;
    const score = maxSpread * uniqueCells * uniqueHours;

    const lons = cl.map(c => c.lo), lats = cl.map(c => c.la);
    const hours = [...new Set(cl.map(c => c.h))].sort((a, b) => a - b);

    /* Direction: majority vote */
    const dirCounts = { "under-warn": 0, "over-warn": 0, "mixed": 0 };
    for (const c of cl) dirCounts[c.direction]++;
    let direction;
    if (dirCounts["under-warn"] > dirCounts["over-warn"]) direction = "under-warn";
    else if (dirCounts["over-warn"] > dirCounts["under-warn"]) direction = "over-warn";
    else direction = "mixed";

    /* Find which zone this cluster belongs to */
    const cLo = (Math.min(...lons) + Math.max(...lons)) / 2;
    const cLa = (Math.min(...lats) + Math.max(...lats)) / 2;
    let region = "unknown";
    for (const [name, bb] of STEEP_ZONES) {
      if (cLo >= bb[0] && cLo <= bb[2] && cLa >= bb[1] && cLa <= bb[3]) { region = name; break; }
    }

    return {
      id: `e${String(idx + 1).padStart(3, "0")}`,
      date: cl[0].date,
      hours: [hours[0], hours[hours.length - 1]],
      region, score, direction,
      cells: uniqueCells, maxSpread,
      bbox: [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)],
    };
  });

  /* Rank: under-warning above equal-score (invariant 3), then by score descending. */
  events.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    const aUW = a.direction === "under-warn" ? 1 : 0;
    const bUW = b.direction === "under-warn" ? 1 : 0;
    return bUW - aUW;
  });

  return events.slice(0, 50);
}

const events = buildEvents();
const eventsDoc = {
  v: 1,
  generated: new Date().toISOString().replace(/\.\d+/, ""),
  physics: { modelHash, sha1: physSha1.slice(0, 12) },
  combos: COMBOS.map(c => c.id),
  note: "under-warning clusters ranked above equal-score (invariant 3)",
  count: events.length,
  events,
};
writeFileSync(`${OUT}/events.json`, JSON.stringify(eventsDoc, null, 1));
log(`\n[${el()}] wrote ${OUT}/events.json — ${events.length} events`);

/* ------------------------------------------------------------------ report */
log(`\n══ SCAN COMPLETE in ${el()} ══`);
log(`dates scanned: ${datesToScan.length}`);
const totalDiv = allDivergence.reduce((a, d) => a + d.results.length, 0);
log(`total divergent cell-hours: ${totalDiv}`);
log(`events: ${events.length}`);
if (events.length) {
  log(`\ntop 5 events:`);
  for (const e of events.slice(0, 5)) {
    log(`  ${e.id} ${e.date} h${e.hours[0]}–${e.hours[1]} ${e.region.padEnd(10)} ` +
        `score ${e.score} spread ${e.maxSpread} cells ${e.cells} ${e.direction}`);
  }
}
log(`\nwrote ${OUT}/  (${datesToScan.length} day files + events.json)`);
