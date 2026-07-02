# SSCOFS current-field ingest — build spec

*Written 2026-07-02 (Fable 5) for the Opus 4.8 build session. All endpoints/gotchas
below were verified live; see memory `project_sscofs_currents` for provenance.
Plan approved by David: build with Opus, validation checkpoint with Fable.*

## Goal

Add NOAA **SSCOFS** surface tidal-current forecasts (the operational Salish Sea
Model, ~370–550 m) as a per-region current FIELD on the wind map — same
byte-pack → R2 → timeline pattern as the HRDPS wind. Phase 1 regions:
`puget-sound`, `san-juan-gulf-islands`, `broughtons-discovery`,
`northern-georgia-strait`. (WCVI later; Haida NOT covered by SSCOFS.)

## Data source (verified)

- Discovery: `https://noaa-nos-ofs-pds.s3.amazonaws.com/?list-type=2&prefix=sscofs/netcdf/YYYY/MM/DD/`
  (public S3 listing, no auth). Cycles: t03z observed; probe for t09/t15/t21.
  Forecast hours f000–f072 (hourly).
- Extraction: **OPeNDAP subsetting** (do NOT download the 1.7 GB regulargrid files):
  `https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS/YYYY/MM/DD/sscofs.tHHz.YYYYMMDD.regulargrid.fNNN.nc.ascii?u_eastward%5B0%5D%5B0%5D%5By1:y2%5D%5Bx1:x2%5D,v_northward%5B0%5D%5B0%5D%5By1:y2%5D%5Bx1:x2%5D`
  - **Gotchas:** curl needs `-g`; brackets MUST be URL-encoded (%5B/%5D — Tomcat 400s
    on literal brackets); a variable may appear only once per constraint.
  - Depth index 0 = surface. `u_eastward`/`v_northward` are true E/N (no rotation).
  - Missing/land cells: check `mask` once (static) or treat NaN/fill as nodata.
  - Measured: 201×301 window ≈ 706 KB ascii ≈ 1 s. Consider `.dods` binary if ascii
    parsing is slow, but ascii at ~50 requests/run is fine.

## Grid (regulargrid product)

Regular lat/lon grid: `lat[iy] = 44.37 + 0.005*iy` (iy 0..1552),
`lon[ix] = -129.53 + 0.005*ix` (ix 0..1518). Verify once at runtime against
Latitude/Longitude corners (single-point OPeNDAP reads) and fail loudly if moved.

Precomputed windows (map REGIONS bboxes → index ranges, ±1 cell padding fine):

| region | bbox [W,S,E,N] | iy (S:N) | ix (W:E) | cells |
|---|---|---|---|---|
| puget-sound | [-123.3, 47.0, -122.2, 48.4] | 526:806 | 1246:1466 | 281×221 |
| san-juan-gulf-islands | [-123.7, 48.3, -122.5, 49.3] | 786:986 | 1166:1406 | 201×241 |
| broughtons-discovery | [-127.3, 49.9, -124.7, 50.95] | 1106:1316 | 446:966 | 211×521 |
| northern-georgia-strait | [-125.4, 49.0, -123.4, 50.2] | 926:1166 | 826:1226 | 241×401 |

## Pipeline (mirror ingest.py; keep in THIS repo — R2 secrets already configured)

- New `sscofs.py` + second job/workflow in `ingest.yml` (cron ~4×/day, offset from
  HRDPS cron; also workflow_dispatch with mode=test|full).
- Find newest complete cycle (S3 listing; require f048 present; today then yesterday).
- Per region per hour: fetch surface u,v window via OPeNDAP → speed kt + direction
  (toward, deg true) → byte-pack **same format as wind bins**: per hour
  rows*cols speed bytes then rows*cols dir bytes; speed byte = kt*25 clamped 0..254
  (0.04 kt resolution, 10.2 kt max), 255 = nodata/land; dir byte = deg*255/360.
  Row 0 = NORTH (flip iy — grid is south-origin!).
- Gzip → R2 prefix `sscofs/` (`manifest.json` same shape as hrdps: run, hours[],
  regions{key:{bbox,cols,rows,file}} — use 49 hours f000–f048 to match wind timeline).
- Test mode: one region, 3 hours, no upload, print stats (valid %, speed range —
  sanity: Puget max should be ~2–6 kt somewhere in the Narrows on a big tide).

## Map integration (hrdps_map.html → deploy repo)

- Load `sscofs/manifest.json` + region .bin when entering a phase-1 region
  (lazy; keep in memory like wind).
- Render: current arrows on a decimated grid (every Nth cell, N by zoom) in cyan,
  distinct from white wind arrows; optional speed tint later. Panel toggle
  "Model currents (SSCOFS)".
- Rings: keep station rings as-is (authoritative); model-field rings can come after
  the Fable validation gate.
- Popup: hovering open water shows model current alongside wind (nearest cell).
- Provenance note: "Model currents = NOAA SSCOFS (Salish Sea Model), experimental
  here until validated — official station predictions remain authoritative."
- Build tag bump; single commit; note GitHub Pages was mid-incident 2026-07-02
  (2 builds already queued: outer-coast + Puget stations).

## Validation gate (Fable 5 checkpoint — BEFORE making the layer visible by default)

Compare SSCOFS nearest-cell surface current vs official NOAA (interval=60) and CHS
predictions at all ~60 station points over 48 h: speed RMSE/correlation, timing of
slack, flood/ebb direction agreement. Expect good agreement in open channels,
degradation at sub-grid pinch points (Deception Pass). Document like
haida_tidal_currents.md (explicit reasoning, figures), then flip the layer on.
