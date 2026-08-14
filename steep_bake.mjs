#!/usr/bin/env node
/* steep_bake.mjs — bake the wave-steepness BAND at 0.56 km, upstream, once per model run.
   =============================================================================================
   WHAT THIS IS. Today unified_map.html computes the steepness band in the browser, on the 2.2 km
   SSCOFS current overview grid: one steepCell() per 2.2 km cell, fed that cell's CENTRE-SAMPLE
   current. That grid is far coarser than the water it is describing — it puts LAND at the centre
   of Gillard and Weynton, and it hands the physics 0.2 kt where the model's own mesh has 7.0 kt.
   Route B (David, digest #133) moves the whole computation upstream and to 0.56 km: score and
   display at the resolution the current data actually has, and ship the ANSWER (a band byte)
   instead of the ingredients. The decisive ratio, measured in #133: the 0.56 km current tile this
   is computed from is 2,193 KB gzipped and the band field it produces is 63 KB — 35× smaller than
   its own input. That is why this is a bake and not a client-side computation.

   WHAT IT EMITS (prefix sscofs/steep/ — a NEW prefix).
     <zone>.src.bin  the per-cell SOURCE byte over the zone's FULL rectangle (row-major, row 0 =
                     NORTH): which current dataset fed this cell, 0 = not scored. Static across
                     hours. It does two jobs at once — it lets the tap readout NAME where its
                     number came from instead of implying a uniformity the inputs do not have
                     (only ~70% of drawn cells sit inside a 0.56 km tile), and its non-zero cells
                     ARE the index for the other two files.
     <zone>.bin      the 0.56 km BAND plane: 1 byte per SCORED cell per hour, in the order the
                     scored cells appear in the source plane. ⚠ Scored cells only, not the whole
                     rectangle — land is 78% of these boxes, and interleaving its constant bytes
                     between the rows of real water costs 2.4× on the wire (measured).
     <zone>.exp.bin  the SPARSE "explain this ring" record: current speed + direction for RINGED
                     cells only. 🔑 It needs NO INDEX — the band plane already says which cells
                     ring, so the client walks the same order and consumes the same number of bytes.
     agg2200.bin     the same field aggregated back to 2.2 km under the ≥25% cumulative rule, as
                     the ZOOM-OUT path (Route A's aggregation, used only as zoom behaviour).
                     Packed the same way, indexed by agg2200.idx.bin over the overview grid.
     manifest.json   hours axis, grid metadata, BOTH upstream run stamps, and physics.js's
                     modelHash so band/model divergence is detectable instead of silent.

   ⛔ WHAT IT DELIBERATELY DOES NOT BAKE, and why (digest #134, measured — do not re-cost):
     · Wave Height (Hamp) and the full tap readout — a continuous scalar at 0.56 km costs 8.3× the
       band and the full-fidelity readout lands at ≈9 MB. The band is cheap because it has five
       values and ~20 fine cells share one 2.5 km wind reading, so it gzips into big flat blocks
       (0.045 B/cell-hour). Nothing else has that property; a DIRECTION plane is 19× the band.
     · Quartering (Q) — structurally unbakeable, independent of size. t.qComps holds up to 17 wave
       components per cell and qRaw(m,heading) recomputes Q for whatever course is dialled in.
       Baking it means a 36-point rose per cell-hour, or losing the rose.
     Both stay client-computed at 2.2 km.

   ⛔ NEVER writes sscofs/field/overview.bin. The COAST map reads that same object, so baking
   anything into it would silently change a live map's currents. New prefix only.

   Usage:
     node steep_bake.mjs --mask steep_out --out steep_out           # full bake, all six zones
     node steep_bake.mjs --zones 2 --hours 6 --no-cache-write       # quick smoke test
   Then: python3 steep_upload.py steep_out                          # boto3 → R2 (CI does this)
*/
import { gunzipSync, gzipSync } from "node:zlib";
import { writeFileSync, readFileSync, existsSync, mkdirSync } from "node:fs";
import { createHash } from "node:crypto";

/* ------------------------------------------------------------------ config */
const R2 = "https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev";
/* The DEPLOYED model, not a repo copy. physics.js is the one source of truth for the band
   (unified_map.html and this baker are its only two consumers), and a baker running against a
   different revision than the map would produce bands the map cannot explain. Fetching the
   deployed file and recording its modelHash makes that divergence visible in the manifest. */
const PHYSICS_URL = "https://dsleaf1.github.io/salish-marine-map/physics.js";

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const has = k => process.argv.includes(k);
const OUT = arg("--out", "steep_out");
const MASKDIR = arg("--mask", "steep_out");
const CACHE = arg("--cache", "");                  // optional R2 byte cache for local iteration
const HOURCAP = +arg("--hours", 0);                // 0 = every shared hour
const ZONESEL = arg("--zones", "");                // e.g. "2" or "2,4"
const PHYSFILE = arg("--physics-file", "");        // local override for offline work
/* Which current dataset wins where several cover the same 0.56 km cell.
   DEFAULT surface,mesh,coarse — this is the combination #133 actually measured (8.40% ring rate,
   63 KB/zone), and the 0.56 km surface tile is read at Depth[0] = 0.0 m, which is the water wind
   waves actually feel. #130's approved "mesh by default" was decided when the alternative was a
   2.2 km DISPLAY, where the mesh's finer geometry outweighed its sigma-layer depth; at 0.56 km
   that trade is different. Flip with --current-priority mesh,surface,coarse and re-measure. */
const PRIORITY = arg("--current-priority", "surface,mesh,coarse").split(",").map(s => s.trim());

const FETCH_SECTORS = 16, FETCH_CAP_KM = 60;
const RING_MIN = 2;                                // band ≥ 2 is what the map draws (a "ring")
const AGG_THRESHOLD = 0.25;                        // #129's cumulative rule, for the zoom-out plane
const MESH_MAX_KM = 1.2;                           // accept radius for the nearest FVCOM element
const B_NODATA = 255, B_NOSEA = 254;               // band-plane sentinels (see the manifest legend)
const SRC = { none: 0, surface: 1, mesh: 2, coarse: 3 };

/* ⚠ keep in sync with unified_map.html:521 (STEEP_ZONES) and steep_mask.mjs. */
const STEEP_ZONES = [
  ["puget",      [-123.30, 47.00, -122.20, 48.35]],
  ["ejdf",       [-123.65, 48.05, -122.70, 48.55]],
  ["sanjuan",    [-123.95, 48.30, -122.50, 49.30]],
  ["georgia",    [-125.25, 48.90, -122.90, 50.15]],
  ["discovery",  [-127.20, 49.90, -124.20, 51.05]],
  ["johnstone",  [-128.15, 50.35, -125.80, 51.10]],
];
/* mesh region ownership order — the map's rule is FIRST bbox that contains the point. */
const MESH_REGIONS = [
  ["puget-sound", [-123.3, 47.0, -122.2, 48.4]],
  ["san-juan-gulf-islands", [-123.95, 48.3, -122.5, 49.3]],
  ["northern-georgia-strait", [-125.4, 49.0, -123.4, 50.2]],
  ["juan-de-fuca", [-125.3, 48.0, -123.25, 48.75]],
  ["broughtons-discovery", [-127.6, 50.15, -124.3, 51.2]],
  ["johnstone-qcs-south", [-129.0, 50.7, -126.3, 52.13]],
];
const FINE_KEYS = ["puget-sound", "san-juan-gulf-islands", "northern-georgia-strait"];

const log = m => process.stderr.write(m + "\n");
const T0 = Date.now();
const el = () => ((Date.now() - T0) / 1000).toFixed(1) + "s";

/* ------------------------------------------------------------------ byte fetch */
if (CACHE && !existsSync(CACHE)) mkdirSync(CACHE, { recursive: true });
async function getBytes(url) {
  const p = CACHE ? CACHE + "/" + createHash("sha1").update(url).digest("hex").slice(0, 16) : null;
  if (p && existsSync(p)) return new Uint8Array(readFileSync(p));
  const r = await fetch(url); if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  let b = new Uint8Array(await r.arrayBuffer());
  if (b[0] === 0x1f && b[1] === 0x8b) b = new Uint8Array(gunzipSync(b));
  if (p) writeFileSync(p, b);
  return b;
}
const getJSON = async u => JSON.parse(Buffer.from(await getBytes(u)).toString("utf8"));
const toMs = h => (typeof h === "string" ? Date.parse(h) : h);

/* ------------------------------------------------------------------ physics.js */
/* Loaded the same way fe_api.mjs loads it for the Node harnesses: read the source, hash it, then
   evaluate it and pull out the exported names. The SENTINEL/DOM checks are fe_api's and catch a
   wrong or DOM-contaminated file loudly rather than producing silently wrong bands. */
const physSrc = PHYSFILE
  ? readFileSync(PHYSFILE, "utf8")
  : await fetch(PHYSICS_URL).then(r => { if (!r.ok) throw new Error(`physics.js HTTP ${r.status}`); return r.text(); });
if (!/function\s+compute\s*\(/.test(physSrc)) throw new Error("physics.js does not define compute() — wrong file");
if (/getElementById|\$\(/.test(physSrc)) throw new Error("physics.js references the DOM — it must stay DOM-free");
const modelHash = createHash("sha256").update(physSrc).digest("hex").slice(0, 12);
const physSha1 = createHash("sha1").update(physSrc).digest("hex");
const { steepCell } = new Function(physSrc + ";return {steepCell};")();
log(`physics.js ${PHYSFILE || PHYSICS_URL} · modelHash ${modelHash} · sha1 ${physSha1.slice(0, 12)}`);

/* ------------------------------------------------------------------ manifests + hour axis */
const [fieldMan, meshMan, windMan] = await Promise.all([
  getJSON(R2 + "/sscofs/field/manifest.json"),
  getJSON(R2 + "/sscofs/meshvel/manifest.json"),
  getJSON(R2 + "/hrdps/manifest.json"),
]);
log(`runs — field ${fieldMan.run} · meshvel ${meshMan.run} · hrdps ${windMan.run}`);

/* The map's own timeline rule (unified_map.html:2391-2396): mesh ∩ field, then ∩ wind. The baked
   plane must live on that same axis or the client cannot index it by its master hour. */
const fH = fieldMan.hours.map(toMs), mH = meshMan.hours.map(toMs), wH = windMan.hours.map(toMs);
const mIdx = new Map(mH.map((v, i) => [v, i])), wIdx = new Map(wH.map((v, i) => [v, i]));
let TIMES = fH.map((v, i) => [v, i]).filter(([v]) => mIdx.has(v) && wIdx.has(v));
if (TIMES.length < 12) throw new Error(`only ${TIMES.length} hours shared by field/mesh/wind — refusing to bake a stub`);
if (HOURCAP) TIMES = TIMES.slice(0, HOURCAP);
log(`${TIMES.length} shared hours ${new Date(TIMES[0][0]).toISOString()} → ${new Date(TIMES.at(-1)[0]).toISOString()}`);

/* ------------------------------------------------------------------ current: 2.2 km overview */
const ov = fieldMan.regions.overview, oCols = ov.cols, oRows = ov.rows, oSpan = oCols * oRows;
const [oW, oS, oE, oN] = ov.bbox;
const oStepLon = (oE - oW) / (oCols - 1), oStepLat = (oN - oS) / (oRows - 1);
const ovBytes = await getBytes(R2 + "/sscofs/field/overview.bin");
if (ovBytes.length !== fH.length * 2 * oSpan) throw new Error("field overview.bin size mismatch");
const ovWet = new Uint8Array(oSpan);
for (let h = 0; h < fH.length; h++) { const o = h * 2 * oSpan; for (let i = 0; i < oSpan; i++) if (ovBytes[o + i] !== 255) ovWet[i] = 1; }
log(`[${el()}] current overview ${oCols}×${oRows} @ ${oStepLat.toFixed(4)}° (${(oStepLat * 111.32).toFixed(2)} km)`);

/* ------------------------------------------------------------------ current: 0.56 km surface tiles */
const LAT0 = 44.37, LON0 = -129.53, DX = 0.005;       // sscofs.py's regulargrid origin
const gy = la => Math.round((la - LAT0) / DX), gx = lo => Math.round((lo - LON0) / DX);
const fine = [];
for (const k of FINE_KEYS) {
  const rg = fieldMan.regions[k]; if (!rg) { log(`  fine ${k}: ABSENT from the field manifest`); continue; }
  const bytes = await getBytes(R2 + "/sscofs/field/" + rg.file);
  const span = rg.cols * rg.rows;
  if (bytes.length !== fH.length * 2 * span) throw new Error(`${k}.bin size mismatch`);
  const [W, S, E, N] = rg.bbox;
  const g = { key: k, bytes, cols: rg.cols, rows: rg.rows, span,
              ix0: gx(W), iy0: gy(S), iy1: gy(S) + rg.rows - 1 };
  // wet-in-any-hour, the map's own water rule
  const wet = new Uint8Array(span);
  for (let h = 0; h < fH.length; h++) { const o = h * 2 * span; for (let i = 0; i < span; i++) if (bytes[o + i] !== 255) wet[i] = 1; }
  g.wet = wet;
  fine.push(g);
  log(`  fine ${k}: ${rg.cols}×${rg.rows} @ ${rg.res_deg}° · ix ${g.ix0}..${g.ix0 + rg.cols - 1} · iy ${g.iy0}..${g.iy1}`);
}
/* lattice index → tile slot. Row 0 of a field tile is NORTH, so the row is measured down from iy1. */
function fineSlot(iy, ix) {
  for (const g of fine) {
    const c = ix - g.ix0, r = g.iy1 - iy;
    if (c < 0 || c >= g.cols || r < 0 || r >= g.rows) continue;
    return { g, j: r * g.cols + c };
  }
  return null;
}

/* ------------------------------------------------------------------ wind: 2.5 km HRDPS */
const wc = windMan.regions.overview, wCols = wc.cols, wRows = wc.rows, wSpan = wCols * wRows;
const [wW, wS, wE, wN] = wc.bbox, wStepLon = (wE - wW) / (wCols - 1), wStepLat = (wN - wS) / (wRows - 1);
const windBytes = await getBytes(R2 + "/hrdps/overview.bin");
if (windBytes.length !== wH.length * 2 * wSpan) throw new Error("hrdps overview.bin size mismatch");
log(`[${el()}] wind overview ${wCols}×${wRows} @ ${wStepLat.toFixed(4)}° (${(wStepLat * 111.32).toFixed(2)} km) ` +
    `— ⚠ ~${Math.round((wStepLat / DX) ** 2)} fine cells share one wind reading`);

/* ------------------------------------------------------------------ static 0.56 km water mask
   Built once by steep_mask.mjs and parked in R2 — it is bathymetry, identical in every cycle, so
   CI downloads it rather than hammering OPeNDAP for 1.5 M cells on every bake. */
if (!existsSync(MASKDIR + "/mask.json")) {
  log(`no ${MASKDIR}/mask.json — pulling the cached mask from R2`);
  if (!existsSync(MASKDIR)) mkdirSync(MASKDIR, { recursive: true });
  writeFileSync(MASKDIR + "/mask.json", Buffer.from(await getBytes(R2 + "/sscofs/steep/static/mask.json")));
  writeFileSync(MASKDIR + "/mask.bin", Buffer.from(await getBytes(R2 + "/sscofs/steep/static/mask.bin")));
}
const maskMeta = JSON.parse(readFileSync(MASKDIR + "/mask.json", "utf8"));
const maskBits = new Uint8Array(readFileSync(MASKDIR + "/mask.bin"));
if (maskBits.length !== maskMeta.rows * maskMeta.cols) throw new Error("mask.bin size mismatch");
/* row 0 = SOUTH in the mask file (grid order), unlike the field tiles. */
const isWater = (iy, ix) => {
  const r = iy - maskMeta.iy0, c = ix - maskMeta.ix0;
  if (r < 0 || r >= maskMeta.rows || c < 0 || c >= maskMeta.cols) return 0;
  return maskBits[r * maskMeta.cols + c];
};
log(`[${el()}] mask ${maskMeta.rows}×${maskMeta.cols} from ${MASKDIR}/mask.bin (built ${maskMeta.built})`);

/* ------------------------------------------------------------------ mesh (used outside the tiles) */
const zoneSel = ZONESEL ? new Set(ZONESEL.split(",").map(Number)) : null;
const ZONES = STEEP_ZONES.map((z, i) => ({ i, name: z[0], bbox: z[1] })).filter(z => !zoneSel || zoneSel.has(z.i));
const needMesh = PRIORITY.includes("mesh");
const mesh = MESH_REGIONS.map(() => null);
if (needMesh) {
  for (let ri = 0; ri < MESH_REGIONS.length; ri++) {
    const [key, bb] = MESH_REGIONS[ri];
    // skip regions that cannot touch any selected zone
    if (!ZONES.some(z => bb[0] <= z.bbox[2] && bb[2] >= z.bbox[0] && bb[1] <= z.bbox[3] && bb[3] >= z.bbox[1])) continue;
    const rm = meshMan.regions[key]; if (!rm) { log(`  mesh ${key}: absent`); continue; }
    const [mj, vb] = await Promise.all([
      getJSON(R2 + "/sscofs/" + rm.mesh + "?v=" + rm.n_elem),
      getBytes(R2 + "/sscofs/meshvel/" + rm.file),
    ]);
    const tris = mj.tris, nodes = mj.nodes, ne = tris.length;
    if (vb.length !== mH.length * 2 * ne) throw new Error(`${key} meshvel size mismatch`);
    const cen = new Float64Array(ne * 2);
    for (let i = 0; i < ne; i++) {
      const t = tris[i];
      cen[i * 2] = (nodes[t[0]][0] + nodes[t[1]][0] + nodes[t[2]][0]) / 3;
      cen[i * 2 + 1] = (nodes[t[0]][1] + nodes[t[1]][1] + nodes[t[2]][1]) / 3;
    }
    mesh[ri] = { key, bb, ne, cen, vel: vb, idx: null };
    log(`  mesh ${key}: ${ne.toLocaleString()} elements`);
  }
}
const ownerOf = (lo, la) => { for (let ri = 0; ri < MESH_REGIONS.length; ri++) { const b = MESH_REGIONS[ri][1];
  if (mesh[ri] && lo >= b[0] && lo <= b[2] && la >= b[1] && la <= b[3]) return ri; } return -1; };
/* Bucket index over element centroids. 0.02° buckets with a 3×3 search: the narrowest guaranteed
   reach is one full bucket of longitude, 1.43 km at 50 N, comfortably past the 1.2 km accept
   radius — so no element inside the radius can be missed at a bucket boundary. */
const BUCK = 0.02;
const bkey = (lo, la) => Math.round(lo / BUCK) + ":" + Math.round(la / BUCK);
function buildMeshIndex() {
  for (let ri = 0; ri < mesh.length; ri++) {
    const M = mesh[ri]; if (!M) continue;
    const mp = new Map(); let kept = 0;
    for (let i = 0; i < M.ne; i++) {
      const lo = M.cen[i * 2], la = M.cen[i * 2 + 1];
      if (!ZONES.some(z => lo >= z.bbox[0] && lo <= z.bbox[2] && la >= z.bbox[1] && la <= z.bbox[3])) continue;
      if (ownerOf(lo, la) !== ri) continue;                    // the map's first-bbox-wins rule
      const k = bkey(lo, la); let a = mp.get(k); if (!a) mp.set(k, a = []); a.push(i); kept++;
    }
    M.idx = mp;
    if (kept) log(`  index ${M.key}: ${kept.toLocaleString()} elements inside the selected zones`);
  }
}
function nearestElem(ri, lo, la) {
  const M = mesh[ri]; if (!M || !M.idx) return -1;
  const cosl = Math.cos(la * Math.PI / 180);
  const b0 = Math.round(lo / BUCK), b1 = Math.round(la / BUCK);
  let best = -1, bd = Infinity;
  for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
    const a = M.idx.get((b0 + dx) + ":" + (b1 + dy)); if (!a) continue;
    for (const i of a) {
      const dlo = (M.cen[i * 2] - lo) * cosl, dla = M.cen[i * 2 + 1] - la, d2 = dlo * dlo + dla * dla;
      if (d2 < bd) { bd = d2; best = i; }
    }
  }
  return (best >= 0 && Math.sqrt(bd) * 111.32 <= MESH_MAX_KM) ? best : -1;
}
if (needMesh) { buildMeshIndex(); log(`[${el()}] mesh indexed`); }

/* ------------------------------------------------------------------ build each zone's fine grid */
const STEP_KM = DX * 111.32, MAX_STEPS = Math.ceil(FETCH_CAP_KM / STEP_KM);
const SEC_STEP = 360 / FETCH_SECTORS;

function buildZone(z) {
  const [W, S, E, N] = z.bbox;
  const ix0 = gx(W), ix1 = gx(E), iy0 = gy(S), iy1 = gy(N);
  const cols = ix1 - ix0 + 1, rows = iy1 - iy0 + 1, span = cols * rows;
  const bbox = [+(LON0 + ix0 * DX).toFixed(4), +(LAT0 + iy0 * DX).toFixed(4),
                +(LON0 + ix1 * DX).toFixed(4), +(LAT0 + iy1 * DX).toFixed(4)];
  const src = new Uint8Array(span);                     // 0 none / 1 surface / 2 mesh / 3 coarse
  const cells = [];                                     // scored cells, in plane order
  const lut = [];                                       // plane index of each scored cell
  const cnt = { water: 0, surface: 0, mesh: 0, coarse: 0, none: 0 };
  for (let r = 0; r < rows; r++) {
    const iy = iy1 - r;                                 // row 0 = NORTH, like the field tiles
    const la = LAT0 + iy * DX;
    for (let c = 0; c < cols; c++) {
      const ix = ix0 + c, lo = LON0 + ix * DX, j = r * cols + c;
      if (!isWater(iy, ix)) continue;
      cnt.water++;
      // --- resolve the current source once, statically: this is a COVERAGE question ---
      let pick = null;
      for (const p of PRIORITY) {
        if (p === "surface") { const s = fineSlot(iy, ix); if (s && s.g.wet[s.j]) { pick = { kind: "surface", s }; break; } }
        else if (p === "mesh") { const ri = ownerOf(lo, la); if (ri >= 0) { const e = nearestElem(ri, lo, la); if (e >= 0) { pick = { kind: "mesh", ri, e }; break; } } }
        else if (p === "coarse") {
          const oc2 = Math.round((lo - oW) / oStepLon), or2 = Math.round((oN - la) / oStepLat);
          if (oc2 >= 0 && oc2 < oCols && or2 >= 0 && or2 < oRows) { const oi = or2 * oCols + oc2; if (ovWet[oi]) { pick = { kind: "coarse", oi }; break; } }
        }
      }
      if (!pick) { cnt.none++; continue; }               // water with no current anywhere: not scored
      src[j] = SRC[pick.kind]; cnt[pick.kind]++;
      let wj = Math.round((lo - wW) / wStepLon), wi = Math.round((wN - la) / wStepLat);
      wj = Math.min(Math.max(wj, 0), wCols - 1); wi = Math.min(Math.max(wi, 0), wRows - 1);
      cells.push({ j, iy, ix, lo, la, pick, wCell: wi * wCols + wj });
      lut.push(j);
    }
  }
  /* DIRECTIONAL FETCH — static geometry, marched ONCE per bake, never per hour and never per run
     input. 16 sectors × up to 108 steps on the 0.56 km mask, 60 km cap. The mask is padded by the
     full cap (steep_mask.mjs), so a ray leaving a zone still meets real land instead of capping. */
  const nc = cells.length;
  const fetchKm = new Float32Array(nc * FETCH_SECTORS);
  for (let s = 0; s < FETCH_SECTORS; s++) {
    const b = s * 2 * Math.PI / FETCH_SECTORS, dN = Math.cos(b), dE = Math.sin(b);
    for (let m = 0; m < nc; m++) {
      let la = cells[m].la, lo = cells[m].lo, f = FETCH_CAP_KM;
      for (let k = 1; k <= MAX_STEPS; k++) {
        la += dN * DX; lo += dE * DX / Math.cos(la * Math.PI / 180);
        if (!isWater(Math.round((la - LAT0) / DX), Math.round((lo - LON0) / DX))) { f = k * STEP_KM; break; }
      }
      fetchKm[m * FETCH_SECTORS + s] = f;
    }
  }
  /* rectangle slot → scored-cell ordinal, so the aggregator can find a fine cell's band without
     storing a rectangle-shaped plane. −1 = not scored. */
  const slot = new Int32Array(span).fill(-1);
  for (let m = 0; m < nc; m++) slot[lut[m]] = m;
  return { ...z, ix0, ix1, iy0, iy1, cols, rows, span, bbox, src, cells, slot, fetchKm, cnt };
}

log(`[${el()}] building zone grids + fetch march…`);
const Z = ZONES.map(z => {
  const b = buildZone(z);
  log(`  zone ${b.i} ${b.name}: ${b.cols}×${b.rows} · ${b.cnt.water.toLocaleString()} water · scored ${b.cells.length.toLocaleString()}` +
      ` (surface ${b.cnt.surface.toLocaleString()} · mesh ${b.cnt.mesh.toLocaleString()} · coarse ${b.cnt.coarse.toLocaleString()}` +
      ` · no current ${b.cnt.none.toLocaleString()})`);
  return b;
});
const totalCells = Z.reduce((a, z) => a + z.cells.length, 0);
log(`[${el()}] ${totalCells.toLocaleString()} scored 0.56 km cells × ${TIMES.length} h = ${(totalCells * TIMES.length / 1e6).toFixed(2)} M steepCell calls`);

/* ------------------------------------------------------------------ score every hour */
for (const z of Z) {
  z.band = new Uint8Array(z.cells.length * TIMES.length).fill(B_NODATA);
  z.expSpd = []; z.expDir = [];                         // per hour, ringed cells only, plane order
  z.ringCellHours = 0; z.scoredCellHours = 0;
}
/* which fine cells belong to each overview cell: the 4×4 lattice block whose centre sample IS the
   overview point (offsets −1..+2, the rounding of the ±0.125/±0.375 sub-cell centres). */
const AGG_OFF = [-1, 0, 1, 2];
/* The zoom-out plane's own static index over the overview grid: which 2.2 km cells have any fine
   sub-cell to aggregate. Built before the hour loop so its packing order is fixed. */
const aggIdx = new Uint8Array(oSpan);
const aggCells = [];
for (const z of Z) for (let or2 = 0; or2 < oRows; or2++) {
  const la = oN - or2 * oStepLat; if (la < z.bbox[1] || la > z.bbox[3]) continue;
  const cy = Math.round((la - LAT0) / DX);
  for (let oc2 = 0; oc2 < oCols; oc2++) {
    const lo = oW + oc2 * oStepLon; if (lo < z.bbox[0] || lo > z.bbox[2]) continue;
    const oi = or2 * oCols + oc2; if (aggIdx[oi]) continue;      // zones overlap; first wins
    const cx = Math.round((lo - LON0) / DX);
    const sub = [];
    for (const dy of AGG_OFF) for (const dx of AGG_OFF) {
      const r = z.iy1 - (cy + dy), c = (cx + dx) - z.ix0;
      if (r < 0 || r >= z.rows || c < 0 || c >= z.cols) continue;
      const m = z.slot[r * z.cols + c]; if (m >= 0) sub.push(m);
    }
    if (!sub.length) continue;
    aggIdx[oi] = 1; aggCells.push({ oi, z, sub });
  }
}
const aggN = aggCells.length;
const agg = new Uint8Array(aggN * TIMES.length).fill(B_NODATA);
log(`[${el()}] zoom-out: ${aggN.toLocaleString()} of ${oSpan.toLocaleString()} 2.2 km cells have fine sub-cells`);

const bandStats = new Float64Array(6);                  // counts of band −1,0,1,2,3,4
let ringCells = 0, scoredCellHours = 0;

for (let hi = 0; hi < TIMES.length; hi++) {
  const [ms, fi] = TIMES[hi];
  const mi = mIdx.get(ms), wi = wIdx.get(ms);
  const cS = fi * 2 * oSpan, wSb = wi * 2 * wSpan;
  for (const z of Z) {
    const nc = z.cells.length, bandOff = hi * nc;
    const spd = [], dir = [];
    for (let m = 0; m < nc; m++) {
      const cell = z.cells[m], p = cell.pick;
      let curKt = -1, curToward = 0;
      if (p.kind === "surface") {
        const off = fi * 2 * p.s.g.span, b = p.s.g.bytes[off + p.s.j];
        if (b !== 255) { curKt = b / 10; curToward = p.s.g.bytes[off + p.s.g.span + p.s.j] * 360 / 255; }
      } else if (p.kind === "mesh") {
        const M = mesh[p.ri], base = mi * 2 * M.ne, b = M.vel[base + p.e];
        if (b !== 255) { curKt = b / 10; curToward = M.vel[base + M.ne + p.e] * 360 / 255; }
      } else {
        const b = ovBytes[cS + p.oi];
        if (b !== 255) { curKt = b / 10; curToward = ovBytes[cS + oSpan + p.oi] * 360 / 255; }
      }
      if (curKt < 0) continue;                          // no current this hour → stays B_NODATA
      const wB = windBytes[wSb + cell.wCell];           // wind covers land: never 255
      const U = wB / 3.6, dirFrom = windBytes[wSb + wSpan + cell.wCell] * 360 / 255;
      const fb = m * FETCH_SECTORS, F = z.fetchKm;
      /* harmonic interpolation between the two adjacent ray-marched sectors — unified_map.html's
         fetchAt, verbatim. Linear interpolation overshoots badly near a channel kink. */
      const fetchAt = beta => {
        const q = (((beta / SEC_STEP) % FETCH_SECTORS) + FETCH_SECTORS) % FETCH_SECTORS;
        const s0 = q | 0, s1 = (s0 + 1) % FETCH_SECTORS, fr = q - s0;
        return 1 / ((1 - fr) / F[fb + s0] + fr / F[fb + s1]);
      };
      const band = steepCell(U, dirFrom, curKt, curToward, fetchAt, "spec").band;
      z.band[bandOff + m] = band < 0 ? B_NOSEA : band;
      scoredCellHours++; z.scoredCellHours++; bandStats[band + 1]++;
      if (band >= RING_MIN) {                            // the sparse explain record, in plane order
        ringCells++; z.ringCellHours++;
        spd.push(Math.min(254, Math.round(curKt * 10)));
        dir.push(Math.round(curToward * 255 / 360) % 256);
      }
    }
    /* per hour: all the speed bytes, then all the direction bytes. Grouping like this (rather than
       interleaved pairs) is what the field tiles do, and for the same reason — speeds run with
       speeds and bearings with bearings, so each half gzips against its own kind. */
    z.expSpd.push(Uint8Array.from(spd)); z.expDir.push(Uint8Array.from(dir));
  }
  /* ---- zoom-out: aggregate the fine bands back to 2.2 km under the ≥25% cumulative rule ----
     🚨 This threshold, approved in #129 and never validated, is the knob now carrying the
     cry-wolf risk: #133 measured that scoring finer REMOVES rings (9.04→8.40%) while aggregating
     back at ≥25% ADDS them (→9.58%). It is the threshold adding marks, not the resolution. */
  const aggOff = hi * aggN;
  for (let a = 0; a < aggN; a++) {
    const A = aggCells[a], bandOff = hi * A.z.cells.length;
    let n = 0; const at = [0, 0, 0, 0, 0];
    for (const m of A.sub) { const v = A.z.band[bandOff + m]; if (v === B_NODATA) continue; n++; if (v !== B_NOSEA) at[v]++; }
    if (!n) continue;
    let painted = 0;
    for (let B = 4; B >= 1; B--) { let k = 0; for (let b = B; b <= 4; b++) k += at[b];
      if (k / n >= AGG_THRESHOLD) { painted = B; break; } }
    agg[aggOff + a] = painted;
  }
  if ((hi + 1) % 10 === 0 || hi === TIMES.length - 1) log(`  [${el()}] hour ${hi + 1}/${TIMES.length}`);
}
log(`[${el()}] scored ${scoredCellHours.toLocaleString()} cell-hours`);

/* ------------------------------------------------------------------ emit */
if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
const gz = b => gzipSync(Buffer.from(b), { level: 9 });
const wire = {};                                       // gzipped sizes, for the report
function emit(name, bytes) {
  writeFileSync(`${OUT}/${name}`, Buffer.from(bytes));
  wire[name] = gz(bytes).length;
  return wire[name];
}

const manifest = {
  model: "wave steepness band, baked at 0.56 km (Route B)",
  generated: new Date().toISOString().replace(/\.\d+/, ""),
  /* BOTH upstream run stamps, plus the mesh run. The bake is triggered off BOTH the HRDPS job and
     the SSCOFS field job (David's option A): wind refreshes 1 h 45 m BEFORE the field job, so a
     field-only trigger would leave the wind layer a run ahead of the rings ~29% of the clock. */
  run_field: fieldMan.run, run_hrdps: windMan.run, run_mesh: meshMan.run,
  trigger: process.env.STEEP_TRIGGER || "manual",
  physics: { url: PHYSFILE ? "local:" + PHYSFILE : PHYSICS_URL, modelHash, sha1: physSha1 },
  res_deg: DX, hours: TIMES.map(t => t[0]),
  band_encoding: { "0-4": "steepness band (0 Very low … 4 Very high)", 254: "scored, negligible sea (physics band −1)", 255: "no current data / land" },
  ring_min_band: RING_MIN,
  current_priority: PRIORITY,
  source_legend: { 0: "not scored", 1: "SSCOFS 0.56 km surface field (Depth 0 m)", 2: "SSCOFS FVCOM mesh element (top sigma layer)", 3: "SSCOFS 2.2 km overview" },
  explain: { note: "per hour: N speed bytes then N direction bytes, N = the number of cells whose band ≥ ring_min_band in that hour, walked in plane order. NO INDEX — the band plane already says which cells ring.", speed_unit: "kt_x10", dir_convention: "toward" },
  packing: "band and explain planes hold SCORED CELLS ONLY, in the order the non-zero bytes of the source plane appear (row-major, row 0 = NORTH). Build that LUT once from the source plane; it indexes both.",
  zoom_out: { file: "agg2200.bin", idx: "agg2200.idx.bin", grid: "index plane is the sscofs/field/overview.bin grid", cols: oCols, rows: oRows, res_deg: +oStepLat.toFixed(4), cells: aggN, rule: `cumulative ≥${AGG_THRESHOLD * 100}% of wet sub-cells at band B or worse`, sub_cells: `${AGG_OFF.length}×${AGG_OFF.length}`, caveat: "the ≥25% rule was approved in digest #129 and has never been validated; #133 measured that it ADDS rings (8.40%→9.58%) where the fine scoring removes them" },
  zones: {},
};

for (const z of Z) {
  const nExp = z.expSpd.reduce((a, x) => a + x.length, 0);
  const exp = new Uint8Array(nExp * 2); let k = 0;
  for (let h = 0; h < z.expSpd.length; h++) { exp.set(z.expSpd[h], k); k += z.expSpd[h].length; exp.set(z.expDir[h], k); k += z.expDir[h].length; }
  const bB = emit(`${z.name}.bin`, z.band), sB = emit(`${z.name}.src.bin`, z.src), eB = emit(`${z.name}.exp.bin`, exp);
  manifest.zones[z.name] = {
    index: z.i, bbox: z.bbox, cols: z.cols, rows: z.rows, res_deg: DX,
    file: `${z.name}.bin`, src: `${z.name}.src.bin`, exp: `${z.name}.exp.bin`,
    scored_cells: z.cells.length, scored_cell_hours: z.scoredCellHours, ring_cell_hours: z.ringCellHours,
    source_counts: { surface: z.cnt.surface, mesh: z.cnt.mesh, coarse: z.cnt.coarse, unscored_water: z.cnt.none },
    bytes_gz: { band: bB, src: sB, exp: eB },
  };
}
const aB = emit("agg2200.bin", agg), aI = emit("agg2200.idx.bin", aggIdx);
manifest.zoom_out.bytes_gz = aB + aI;
writeFileSync(`${OUT}/manifest.json`, JSON.stringify(manifest, null, 1));
wire["manifest.json"] = gz(Buffer.from(JSON.stringify(manifest))).length;

/* ------------------------------------------------------------------ optional: score TODAY's path
   The map as it stands today: one steepCell per 2.2 km overview water cell inside a zone, fed that
   cell's CENTRE-SAMPLE current and a fetch marched on the 2.2 km water mask. Scoring it here, from
   the same run and the same physics, is the only way to say whether the fine bake ADDS or REMOVES
   marks — and #133's expectation is that it does opposite things in different zones (it removed
   them in San Juan; the Broughtons, with the worst under-feed and no 0.56 km tile, was expected to
   add them). ⚠ this is a MEASUREMENT aid, not part of the product: skip it in CI. */
const cmp = {};
if (has("--compare")) {
  log(`[${el()}] scoring today's 2.2 km path for comparison…`);
  const cMask = ovWet;
  const cStepKm = oStepLat * 111.32, cMax = Math.ceil(FETCH_CAP_KM / cStepKm);
  for (const z of Z) {
    const cc = [];
    for (let r = 0; r < oRows; r++) { const la = oN - r * oStepLat; if (la < z.bbox[1] || la > z.bbox[3]) continue;
      for (let c = 0; c < oCols; c++) { const lo = oW + c * oStepLon; if (lo < z.bbox[0] || lo > z.bbox[2]) continue;
        const oi = r * oCols + c; if (!cMask[oi]) continue;
        let wj = Math.round((lo - wW) / wStepLon), wi2 = Math.round((wN - la) / wStepLat);
        cc.push({ oi, lo, la, wCell: Math.min(Math.max(wi2, 0), wRows - 1) * wCols + Math.min(Math.max(wj, 0), wCols - 1) }); } }
    const F = new Float32Array(cc.length * FETCH_SECTORS);
    for (let s = 0; s < FETCH_SECTORS; s++) { const b = s * 2 * Math.PI / FETCH_SECTORS, dN = Math.cos(b), dE = Math.sin(b);
      for (let m = 0; m < cc.length; m++) { let la = cc[m].la, lo = cc[m].lo, f = FETCH_CAP_KM;
        for (let k = 1; k <= cMax; k++) { la += dN * oStepLat; lo += dE * oStepLat / Math.cos(la * Math.PI / 180);
          const rr = Math.round((oN - la) / oStepLat), c2 = Math.round((lo - oW) / oStepLon);
          if (rr < 0 || rr >= oRows || c2 < 0 || c2 >= oCols) break;
          if (!cMask[rr * oCols + c2]) { f = k * cStepKm; break; } }
        F[m * FETCH_SECTORS + s] = f; } }
    let ring = 0, n = 0;
    for (const [ms, fi] of TIMES) {
      const cS = fi * 2 * oSpan, wSb = wIdx.get(ms) * 2 * wSpan;
      for (let m = 0; m < cc.length; m++) {
        const b = ovBytes[cS + cc[m].oi]; if (b === 255) continue;
        const wB = windBytes[wSb + cc[m].wCell], fb = m * FETCH_SECTORS;
        const fa = beta => { const q = (((beta / SEC_STEP) % FETCH_SECTORS) + FETCH_SECTORS) % FETCH_SECTORS;
          const s0 = q | 0, s1 = (s0 + 1) % FETCH_SECTORS, fr = q - s0;
          return 1 / ((1 - fr) / F[fb + s0] + fr / F[fb + s1]); };
        const bd = steepCell(wB / 3.6, windBytes[wSb + wSpan + cc[m].wCell] * 360 / 255, b / 10,
                             ovBytes[cS + oSpan + cc[m].oi] * 360 / 255, fa, "spec").band;
        n++; if (bd >= RING_MIN) ring++;
      }
    }
    cmp[z.name] = { cells: cc.length, cellHours: n, ring, rate: n ? 100 * ring / n : NaN };
  }
  log(`[${el()}] comparison done`);
}

/* ------------------------------------------------------------------ report */
const KB = n => (n / 1024).toFixed(1) + " KB";
const cellHours = totalCells * TIMES.length;
log("");
log(`══ BAKE COMPLETE in ${el()} ══`);
log(`hours ${TIMES.length} · scored cells ${totalCells.toLocaleString()} · cell-hours ${cellHours.toLocaleString()}`);
log(`ring rate ${(100 * ringCells / scoredCellHours).toFixed(2)}% of scored cell-hours (band ≥ ${RING_MIN})`);
log(`band mix  −1 ${bandStats[0]} · 0 ${bandStats[1]} · 1 ${bandStats[2]} · 2 ${bandStats[3]} · 3 ${bandStats[4]} · 4 ${bandStats[5]}`);
log("");
log("PER ZONE — what it costs and how many marks it draws:");
log(`  ${"zone".padEnd(10)} ${"cells".padStart(8)} ${"src mix (surf/mesh/coarse)".padStart(28)} ${"ring %".padStart(7)}` +
    (has("--compare") ? ` ${"today %".padStart(8)} ${"change".padStart(8)}` : ""));
let tot = 0, totBand = 0, totExp = 0, totSrc = 0;
for (const z of Z) {
  const m = manifest.zones[z.name], g = m.bytes_gz;
  totBand += g.band; totExp += g.exp; totSrc += g.src;
  const rate = z.scoredCellHours ? 100 * z.ringCellHours / z.scoredCellHours : NaN;
  const c = cmp[z.name];
  log(`  ${z.name.padEnd(10)} ${z.cells.length.toLocaleString().padStart(8)} ` +
      `${`${z.cnt.surface} / ${z.cnt.mesh} / ${z.cnt.coarse}`.padStart(28)} ${rate.toFixed(2).padStart(7)}` +
      (c ? ` ${c.rate.toFixed(2).padStart(8)} ${((rate - c.rate) >= 0 ? "+" : "") + (rate - c.rate).toFixed(2).padStart(7)}` : ""));
}
log("");
log("WIRE (gzip −9, what the browser actually downloads):");
for (const z of Z) {
  const g = manifest.zones[z.name].bytes_gz;
  log(`  ${z.name.padEnd(10)} band ${KB(g.band).padStart(9)} · explain ${KB(g.exp).padStart(9)} · source ${KB(g.src).padStart(8)}` +
      `   (band ${(g.band / z.scoredCellHours).toFixed(3)} B/scored cell-hour)`);
}
tot = totBand + totExp + totSrc + aB + aI + wire["manifest.json"];
log(`  ${"zoom-out".padEnd(10)} ${KB(aB)} + index ${KB(aI)}   ·   manifest ${KB(wire["manifest.json"])}`);
log(`  ${"TOTAL".padEnd(10)} ${KB(tot)}  =  band ${KB(totBand)} + explain ${KB(totExp)} + source ${KB(totSrc)} + zoom-out ${KB(aB + aI)}`);
log("");
log(`🚨 THE SPARSE EXPLAIN RECORD, MEASURED: ${KB(totExp)} gzipped for ${ringCells.toLocaleString()} ringed cell-hours`);
log(`   = ${(totExp / ringCells).toFixed(3)} B per ringed cell-hour (raw is 2.000 — gzip saves only ${(100 - 100 * totExp / (2 * ringCells)).toFixed(0)}%).`);
log(`   ⚠ it scales with the RING RATE, and this run's is ${(100 * ringCells / scoredCellHours).toFixed(2)}%. At #133's 8.40% the same`);
log(`   record would be ≈ ${KB(6.28e6 * 0.084 * (totExp / ringCells))} — i.e. #134's ~0.5–0.7 MB arithmetic was NOT far off.`);
log(`   source byte ${KB(totSrc)} = ${(100 * totSrc / tot).toFixed(2)}% of the wire`);
log("");
log(`wrote ${OUT}/  → upload with:  python3 steep_upload.py ${OUT}`);
