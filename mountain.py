#!/usr/bin/env python3
"""
HRDPS mountain bake for the inReach bot's `mt` command: wind and temperature aloft, freezing level,
precipitation.

Source: ECCC HRDPS Continental 2.5 km on the MSC Datamart (the same runs ingest.py uses), fields on
pressure levels 1000..550 hPa:
  WIND_ISBL / WDIR_ISBL   wind speed (m/s) and direction it blows FROM (true degrees)
  HGT_ISBL                geopotential height of the level (m)
  TMP_ISBL                temperature (K)
plus APCP_Sfc (precipitation accumulated since the run started, mm) and HGT_Sfc (model terrain, m).

Only the hours the bot reports are baked: local 04 08 12 16 20 for every day the run reaches, plus a
14:00 temperature-only sample so the daily freezing-level maximum and temperature high catch the
afternoon peak.

Per time step, per cell:
  wind at fixed heights 500..4500 m ASL (every 500 m), linearly interpolated between pressure
      levels using each level's own height. Values are kept even where a height is below the model
      terrain (the model extrapolates underground); the bot refuses those using the terrain byte.
  temperature at the same fixed heights, interpolated the same way (every step, 14:00 included).
  freezing level: the HIGHEST 0 C crossing in the column (m ASL). 0 = whole column below freezing.
  precipitation: mm in the 4 hours ending at the step.

Output (R2, prefix mt/): manifest.json + {region}.{run hour}.bin, UNCOMPRESSED and cell-major so
the bot reads one cell with a single HTTP Range request:
  record = [terrain] + for each step: ([speed, dir] for each height) + (temp for each height)
                                      + [freezing level] + [precip]
  cell offset = cell_index * record_len      (cell_index = row * cols + col, row 0 = north)
Byte encodings are in the manifest ("encoding"). 255 = no data everywhere.

Local smoke test (needs eccodes + deps):  python mountain.py --no-upload --max-steps 2
"""
import os, re, sys, json, argparse, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np
import ingest as I                      # http_get, list_links, read_grib, build_interp, regrid, bbox_of, res_of, r2_client

BASE = "https://dd.weather.gc.ca/{date}/WXO-DD/model_hrdps/continental/2.5km"
REGIONS = ["coast-overview"]            # Puget Sound -> Dixon Entrance; all Pacific time
TZ = "America/Vancouver"                # slot hours are local to this zone (add a zone per region later).
                                        # NB British Columbia keeps UTC-7 all year from 2026; Washington
                                        # still falls back, so in winter these slots are 1 h early there.
LEVELS = [1000, 950, 900, 850, 800, 750, 700, 650, 600, 550]    # hPa, low -> high
HEIGHTS = list(range(500, 4501, 500))                           # m ASL
SLOTS = (4, 8, 12, 16, 20)              # local hours the bot reports (wind + freezing level + precip)
FZ_EXTRA = (14,)                        # local hours baked for freezing level only
PRECIP_H = 4                            # precipitation is the total over this many hours before a slot
MAX_FHOUR = 48
TERRAIN_M, FZ_M = 20, 20                # metres per byte step
TEMP_OFFSET = 60                        # temperature byte = (deg C + 60) * 2
STEP_LEN = 3 * len(HEIGHTS) + 2         # per step: (speed, dir) and temp per height, freezing level, precip
FZ_ABOVE = 254                          # freezing level above the top pressure level
PREFIX = "mt"

def log(*a): print(*a, file=sys.stderr, flush=True)

# ---------------------------------------------------------------- pure maths (unit-tested)
def plan_steps(run_dt, tzname=TZ):
    """-> [(valid_utc, forecast_hour, has_wind)] for every local slot hour the run covers."""
    tz = ZoneInfo(tzname)
    d0 = run_dt.astimezone(tz).date()
    out = []
    for dd in range(-1, 4):
        day = d0 + dt.timedelta(days=dd)
        for h in sorted(SLOTS + FZ_EXTRA):
            t = dt.datetime(day.year, day.month, day.day, h, tzinfo=tz).astimezone(dt.timezone.utc)
            fh = (t - run_dt).total_seconds() / 3600
            if 0 <= fh <= MAX_FHOUR and fh == int(fh):
                out.append((t, int(fh), h in SLOTS))
    return out

def to_heights(H, spd, wdir, heights=HEIGHTS):
    """Wind on pressure levels -> wind at fixed heights.
    H, spd, wdir: arrays [level, cell], levels ordered low -> high. -> (speed, dir_from) [height, cell],
    NaN where the height is outside the column covered by the levels."""
    rad = np.radians(wdir)
    u, v = -spd * np.sin(rad), -spd * np.cos(rad)
    n = H.shape[1]
    us = np.full((len(heights), n), np.nan, "float32")
    vs = np.full((len(heights), n), np.nan, "float32")
    for k, z in enumerate(heights):
        for l in range(H.shape[0] - 1):
            lo, hi = H[l], H[l + 1]
            m = (lo <= z) & (z <= hi) & (hi > lo)
            if m.any():
                w = (z - lo[m]) / (hi[m] - lo[m])
                us[k, m] = u[l][m] * (1 - w) + u[l + 1][m] * w
                vs[k, m] = v[l][m] * (1 - w) + v[l + 1][m] * w
    speed = np.hypot(us, vs)
    direction = np.degrees(np.arctan2(-us, -vs)) % 360
    return speed, direction

def interp_heights(H, X, heights=HEIGHTS):
    """A scalar on pressure levels -> the same scalar at fixed heights, linear in height.
    H, X: arrays [level, cell], levels low -> high. -> [height, cell]; NaN outside the column."""
    n = H.shape[1]
    out = np.full((len(heights), n), np.nan, "float32")
    for k, z in enumerate(heights):
        for l in range(H.shape[0] - 1):
            lo, hi = H[l], H[l + 1]
            m = (lo <= z) & (z <= hi) & (hi > lo)
            if m.any():
                w = (z - lo[m]) / (hi[m] - lo[m])
                out[k, m] = X[l][m] * (1 - w) + X[l + 1][m] * w
    return out

def freezing_level(H, T):
    """Highest 0 C crossing. H [level, cell] in m, T [level, cell] in deg C, levels low -> high.
    -> metres; 0 where the whole column is below freezing; +inf where it is above freezing all the
    way to the top level."""
    n = H.shape[1]
    out = np.full(n, np.nan, "float32")
    for l in range(H.shape[0] - 2, -1, -1):                 # top pair first: the highest crossing wins
        lo_t, hi_t = T[l], T[l + 1]
        m = np.isnan(out) & (lo_t >= 0) & (hi_t < 0)
        if m.any():
            out[m] = H[l][m] + (lo_t[m] / (lo_t[m] - hi_t[m])) * (H[l + 1][m] - H[l][m])
    none = np.isnan(out)
    out[none & (T[-1] >= 0)] = np.inf
    out[none & (T[-1] < 0)] = 0.0
    bad = ~np.isfinite(T).all(axis=0) | ~np.isfinite(H).all(axis=0)
    out[bad] = np.nan
    return out

def enc_precip(mm):
    """0-100 = tenths of a mm (0-10 mm); 101-254 = 10 mm + whole mm (11-164 mm); 255 = no data."""
    mm = np.asarray(mm, "float32")
    b = np.where(mm <= 10, np.round(mm * 10), np.clip(np.round(mm) + 90, 101, 254))
    return np.where(np.isfinite(mm), np.clip(b, 0, 254), 255).astype("uint8")

def enc_scaled(x, step):
    """Whole steps of `step`, 0..253; 255 = no data. (254 is reserved by the caller.)"""
    x = np.asarray(x, "float32")
    return np.where(np.isfinite(x), np.clip(np.round(x / step), 0, 253), 255).astype("uint8")

def enc_wind(speed_kmh, direction):
    s = np.where(np.isfinite(speed_kmh), np.clip(np.round(speed_kmh), 0, 254), 255).astype("uint8")
    d = np.where(np.isfinite(direction), np.round((direction % 360) * 255.0 / 360.0) % 255, 0).astype("uint8")
    return s, d

def enc_temp(c):
    """deg C -> (C + 60) * 2, i.e. half degrees from -60 to +67 C; 255 = no data."""
    c = np.asarray(c, "float32")
    return np.where(np.isfinite(c), np.clip(np.round((c + TEMP_OFFSET) * 2), 0, 254), 255).astype("uint8")

def step_columns(H, T, spd, wdir, precip_mm):
    """One time step's byte columns for a region, in record order. spd/wdir None = freezing-level-only
    step (wind bytes 255; temperatures are still written). precip_mm None = no precipitation."""
    n = H.shape[1]
    cols = []
    if spd is None:
        cols += [np.full(n, 255, "uint8")] * (2 * len(HEIGHTS))
    else:
        s, d = to_heights(H, spd * 3.6, wdir)
        for k in range(len(HEIGHTS)):
            cols += list(enc_wind(s[k], d[k]))
    tc = interp_heights(H, T)
    cols += [enc_temp(tc[k]) for k in range(len(HEIGHTS))]
    fz = freezing_level(H, T)
    fzb = enc_scaled(np.where(np.isinf(fz), np.nan, fz), FZ_M)
    fzb[np.isinf(fz)] = FZ_ABOVE
    cols.append(fzb)
    cols.append(np.full(n, 255, "uint8") if precip_mm is None else enc_precip(precip_mm))
    return cols

# ---------------------------------------------------------------- Datamart
NEEDED_AT_END = ["_WIND_AGL-10m_", f"_WDIR_ISBL_{LEVELS[-1]:04d}_", f"_TMP_ISBL_{LEVELS[-1]:04d}_", "_APCP_Sfc_"]

def find_latest_run():
    """Newest run whose LAST forecast hour already holds every kind of file this bake needs."""
    now = dt.datetime.now(dt.timezone.utc)
    for d in (0, 1):
        b = BASE.format(date=(now - dt.timedelta(days=d)).strftime("%Y%m%d"))
        try:
            runs = sorted({l.strip("/") for l in I.list_links(b + "/") if re.fullmatch(r"\d{2}/", l)}, reverse=True)
        except Exception:
            continue
        for run in runs:
            try:
                files = I.list_links(f"{b}/{run}/{MAX_FHOUR:03d}/")
            except Exception:
                continue
            if all(any(tag in f and f.endswith(".grib2") for f in files) for tag in NEEDED_AT_END):
                return b, run, next(f for f in files if "_WIND_AGL-10m_" in f and f.endswith(".grib2"))
    raise RuntimeError("no complete HRDPS run with pressure-level fields found")

class Run:
    """Downloads and regrids fields of one run; remembers the interpolators."""
    def __init__(self, base, run, sample):
        self.base, self.run, self.sample, self.interps = base, run, sample, {}
        self.bytes = 0

    def field(self, label, fh):
        """-> {region: values[cell]} for one GRIB2 field at forecast hour fh."""
        url = f"{self.base}/{self.run}/{fh:03d}/" + I.file_for(self.sample, label, fh)
        buf = I.http_get(url, binary=True)
        self.bytes += len(buf)
        vals, lat, lon = I.read_grib(buf)
        if not self.interps:
            for k in REGIONS:
                self.interps[k] = I.build_interp(lat, lon, I.bbox_of(k), I.res_of(k))
                log(f"  region {k}: {self.interps[k]['cols']}x{self.interps[k]['rows']}")
        return {k: I.regrid(vals, self.interps[k]) for k in REGIONS}

    def levels(self, name, fh):
        """-> {region: array[level, cell]}"""
        per = [self.field(f"{name}_ISBL_{p:04d}", fh) for p in LEVELS]
        return {k: np.stack([x[k] for x in per]) for k in REGIONS}

def model_terrain(R, fh):
    """Model surface height. It never changes, so any forecast hour that publishes it will do."""
    last = None
    for h in (fh, 0, 1, 3, 6):
        try:
            return R.field("HGT_Sfc", h)
        except Exception as e:
            last = e
    raise RuntimeError(f"HGT_Sfc not found: {last}")

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true", help="write to ./out instead of R2")
    ap.add_argument("--max-steps", type=int, default=0, help="limit time steps (testing)")
    args = ap.parse_args()

    base, run, sample = find_latest_run()
    run_dt = I.run_datetime(sample)
    steps = plan_steps(run_dt)
    if args.max_steps:
        steps = steps[: args.max_steps]
    log(f"HRDPS run {run_dt:%Y-%m-%dT%HZ} | {len(steps)} steps | {sample}")

    R = Run(base, run, sample)
    apcp_cache = {}
    def apcp(fh):
        if fh == 0:
            return None                                     # nothing has fallen yet at hour 0
        if fh not in apcp_cache:
            apcp_cache[fh] = R.field("APCP_Sfc", fh)
        return apcp_cache[fh]

    columns = {k: [] for k in REGIONS}                      # per region: list of byte columns
    times, has_wind, terrain = [], [], None
    for t, fh, wind in steps:
        try:
            H = R.levels("HGT", fh)
            T = R.levels("TMP", fh)
            S = R.levels("WIND", fh) if wind else None
            D = R.levels("WDIR", fh) if wind else None
            if terrain is None:
                terrain = model_terrain(R, fh)
            pp = None
            if wind and fh - PRECIP_H >= 0:
                a1, a0 = apcp(fh), apcp(fh - PRECIP_H)
                pp = {k: a1[k] - (a0[k] if a0 else 0) for k in REGIONS}
                worst = min(float(np.nanmin(pp[k])) for k in REGIONS)
                if worst < -0.5:                            # APCP_Sfc must only ever grow
                    log(f"  hour {fh:03d}: APCP not cumulative (min diff {worst:.1f} mm); precip left empty")
                    pp = None
                else:
                    pp = {k: np.maximum(pp[k], 0) for k in REGIONS}
        except Exception as e:
            log(f"  hour {fh:03d}: missing ({e}); skipping")
            continue
        for k in REGIONS:
            columns[k] += step_columns(H[k], T[k] - 273.15, S[k] if wind else None, D[k] if wind else None,
                                       pp[k] if pp else None)
        times.append(t.strftime("%Y-%m-%dT%H:%M:%SZ")); has_wind.append(bool(wind))
        log(f"  hour {fh:03d} ({t.astimezone(ZoneInfo(TZ)):%a %H:%M} local){'' if wind else ' fz only'}: ok, "
            f"{R.bytes / 1e6:.0f} MB so far")
    if not times or not any(has_wind):
        raise RuntimeError("no wind steps ingested")

    step_len = STEP_LEN
    rec_len = 1 + len(times) * step_len
    blobs = {}
    for k in REGIONS:
        tb = enc_scaled(terrain[k], TERRAIN_M)
        rec = np.stack([tb] + columns[k], axis=1)           # [cell, record byte] -> cell-major on disk
        assert rec.shape == (R.interps[k]["cols"] * R.interps[k]["rows"], rec_len), rec.shape
        blobs[k] = rec.tobytes()
        i = next(j for j, w in enumerate(has_wind) if w)
        k2000 = HEIGHTS.index(2000)
        sp = rec[:, 1 + i * step_len + 2 * k2000]
        tt = rec[:, 1 + i * step_len + 2 * len(HEIGHTS) + k2000]
        fz = rec[:, 1 + i * step_len + 3 * len(HEIGHTS)]
        ok = sp[sp != 255]
        tok = tt[tt != 255] / 2.0 - TEMP_OFFSET
        msg = (f"{k}: {len(times)} steps, record {rec_len} B, file {len(blobs[k]) / 1e6:.1f} MB, terrain max "
               f"{int(tb[tb != 255].max()) * TERRAIN_M} m, first step 2000 m wind mean "
               f"{ok.mean() if ok.size else float('nan'):.0f} max {ok.max() if ok.size else 0} km/h, 2000 m temp "
               f"{tok.min() if tok.size else float('nan'):.0f} to {tok.max() if tok.size else float('nan'):.0f} C, "
               f"freezing level median {float(np.median(fz[fz < FZ_ABOVE])) * FZ_M if (fz < FZ_ABOVE).any() else float('nan'):.0f} m, "
               f"downloaded {R.bytes / 1e6:.0f} MB")
        log(msg)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=mt::{msg}", flush=True)

    files = {k: f"{k}.{run_dt:%H}.bin" for k in REGIONS}    # named by run hour: the manifest never
    manifest = {                                            # points at a file that is being replaced
        "model": "ECCC HRDPS Continental 2.5 km", "run": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tz": TZ, "slots_local": list(SLOTS), "fz_extra_local": list(FZ_EXTRA),
        "steps": times, "wind": has_wind,
        # the same steps as local wall-clock "YYYY-MM-DDTHH": the bot matches slots on these, so it
        # never depends on its own machine's time-zone tables agreeing with this one's
        "steps_local": [dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
                        .astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%dT%H") for t in times], "heights_m": HEIGHTS, "levels_hpa": LEVELS,
        "precip_hours": PRECIP_H, "nodata": 255, "step_len": step_len, "record_len": rec_len,
        "encoding": {
            "record": "terrain, then per step: (speed, dir) per height, temp per height, freezing level, precip",
            "terrain": f"m ASL / {TERRAIN_M}", "speed": "km/h", "dir": "from, 0..360 -> 0..255",
            "temp": f"deg C = byte / 2 - {TEMP_OFFSET}",
            "freezing_level": f"m ASL / {FZ_M}; {FZ_ABOVE} = above the {LEVELS[-1]} hPa level",
            "precip": "0-100 tenths of mm; 101-254 = whole mm + 90",
        },
        "regions": {k: {"bbox": I.bbox_of(k), "cols": R.interps[k]["cols"], "rows": R.interps[k]["rows"],
                        "res_deg": I.res_of(k), "file": files[k]} for k in REGIONS},
    }

    if args.no_upload:
        os.makedirs("out", exist_ok=True)
        json.dump(manifest, open("out/mt_manifest.json", "w"), indent=1)
        for k in REGIONS:
            open(f"out/mt_{files[k]}", "wb").write(blobs[k])
        log("wrote ./out")
        return
    c = I.r2_client()
    bucket = os.environ["R2_BUCKET"]
    for k in REGIONS:                                       # not gzipped: the bot uses Range requests
        c.put_object(Bucket=bucket, Key=f"{PREFIX}/{files[k]}", Body=blobs[k],
                     ContentType="application/octet-stream", CacheControl="public, max-age=900")
    c.put_object(Bucket=bucket, Key=f"{PREFIX}/manifest.json", Body=json.dumps(manifest).encode(),
                 ContentType="application/json", CacheControl="public, max-age=300")   # manifest last
    log(f"uploaded {PREFIX}/ ({len(REGIONS)} region file(s))")

if __name__ == "__main__":
    main()
