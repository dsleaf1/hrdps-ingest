#!/usr/bin/env python3
"""
HRDPS wind ingest for the BC-coast kayaker map.

Two models (select with --model):
  continental : HRDPS Continental 2.5 km, operational (dd.weather.gc.ca), 00/06/12/18Z, 0-48h
  hrdps_west  : HRDPS West 1 km nest, experimental (dd.alpha.weather.gc.ca), 00/12Z only, 1-48h
                footprint lat 45.9..60.3, lon -134.6..-109.5 (covers all 6 kayak regions)

Pipeline: find latest complete run -> download 10 m WIND (speed) + WDIR (direction) GRIB2
(native rotated grid) -> nearest-neighbour regrid each region to a regular lat/lon grid at the
model's native spacing -> byte-pack speed (km/h, 255=nodata) + direction (0..360->0..255) per
hour -> gzip + upload per-region binaries + manifest.json to Cloudflare R2 under the model prefix.

Local smoke test (needs eccodes + deps):
  python ingest.py --model hrdps_west --no-upload --regions haida-gwaii --max-hours 3
"""
import os, re, sys, gzip, json, time, argparse, tempfile
import datetime as dt
import urllib.request
import numpy as np

MODELS = {
    "continental": {
        "name": "HRDPS Continental 2.5 km (ECCC)",
        "base": "https://dd.weather.gc.ca/{date}/WXO-DD/model_hrdps/continental/2.5km",   # date-based layout (ECCC migration 2026-07)
        "res": 0.0225, "hours": list(range(0, 49)),
        "wind_label": "WIND_AGL-10m", "wdir_label": "WDIR_AGL-10m",
        "fhour_re": r"PT\d{3}H", "fhour_fmt": "PT{:03d}H",
        "prefix": "hrdps",
    },
    "hrdps_west": {
        "name": "HRDPS West 1 km nest (ECCC, experimental)",
        "base": "https://dd.alpha.weather.gc.ca/model_hrdps/west/1km/grib2",
        "res": 0.009, "hours": list(range(1, 49)),     # west nest has no hour 000
        "wind_label": "WIND_TGL_10", "wdir_label": "WDIR_TGL_10",
        "fhour_re": r"P\d{3}-00", "fhour_fmt": "P{:03d}-00",
        "prefix": "hrdps1km",
    },
}
M = MODELS["continental"]      # set in main()
RES = M["res"]

# region key -> bbox [lonW, latS, lonE, latN]  (must match the map app's REGIONS)
REGIONS = {
    "haida-gwaii":             [-133.6, 50.7, -128.3, 54.5],   # full Dixon–Hecate–QCS transect (2026-07-01)
    "west-coast-vancouver-i":  [-128.7, 48.3, -124.8, 50.9],
    "broughtons-discovery":    [-127.3, 49.9, -124.7, 50.95],
    "northern-georgia-strait": [-125.4, 49.0, -123.4, 50.2],
    "san-juan-gulf-islands":   [-123.95, 48.3, -122.5, 49.3],   # widened W for Dodd Narrows/Gabriola (2026-07-03)
    "puget-sound":             [-123.3, 47.0, -122.2, 48.4],
}
REGION_NAMES = {
    "haida-gwaii": "Haida Gwaii", "west-coast-vancouver-i": "West Coast Vancouver I.",
    "broughtons-discovery": "Broughtons & Discovery", "northern-georgia-strait": "Northern Georgia Strait",
    "san-juan-gulf-islands": "San Juan & Gulf Islands", "puget-sound": "Puget Sound",
}

def log(*a): print(*a, file=sys.stderr, flush=True)

# ---------- Datamart discovery ----------
def http_get(url, binary=False, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hrdps-ingest"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read() if binary else r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e; time.sleep(2 * (i + 1))
    raise last

def list_links(url):
    return re.findall(r'href="([^"?][^"]*)"', http_get(url))

def candidate_bases():
    """Resolve M['base']; a {date} placeholder expands to today then yesterday (UTC)."""
    if "{date}" not in M["base"]:
        return [M["base"]]
    now = dt.datetime.now(dt.timezone.utc)
    return [M["base"].format(date=(now - dt.timedelta(days=d)).strftime("%Y%m%d")) for d in (0, 1)]

def find_latest_run():
    """Return (base, run_hh, sample_wind_filename) for the newest run whose hour 048 exists."""
    for b in candidate_bases():
        try:
            runs = sorted({l.strip("/") for l in list_links(b + "/") if re.fullmatch(r"\d{2}/", l)}, reverse=True)
        except Exception:
            continue
        for run in runs:
            try:
                files = list_links(f"{b}/{run}/048/")
            except Exception:
                continue
            wind = [f for f in files if M["wind_label"] in f and f.endswith(".grib2")]
            if wind:
                return b, run, wind[0]
    raise RuntimeError("No complete run found for model " + M["name"])

def run_datetime(sample_filename):
    # date+run token "YYYYMMDDTHHZ" appears at the start (continental) or mid-name (west)
    m = re.search(r"(\d{8})T(\d{2})Z", sample_filename)
    return dt.datetime.strptime(m.group(1), "%Y%m%d").replace(hour=int(m.group(2)), tzinfo=dt.timezone.utc)

def file_for(sample, label, hhh):
    f = sample.replace(M["wind_label"], label)              # sample is the WIND file
    f = re.sub(M["fhour_re"], M["fhour_fmt"].format(hhh), f)
    return f

# ---------- GRIB decode ----------
def read_grib(buf):
    import xarray as xr
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(buf); path = tf.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        var = list(ds.data_vars)[0]
        vals = np.asarray(ds[var].values, dtype="float32")
        lat = np.asarray(ds["latitude"].values, dtype="float32")
        lon = np.asarray(ds["longitude"].values, dtype="float32")
        lon = ((lon + 180.0) % 360.0) - 180.0
        return vals, lat, lon
    finally:
        try: os.unlink(path)
        except OSError: pass

# ---------- regridding ----------
def region_axes(bbox):
    W, S, E, N = bbox
    cols = int(round((E - W) / RES)) + 1
    rows = int(round((N - S) / RES)) + 1
    return cols, rows, np.linspace(W, E, cols), np.linspace(N, S, rows)   # row 0 = north

def build_interp(lat, lon, bbox):
    from scipy.spatial import cKDTree
    W, S, E, N = bbox
    cosm = np.cos(np.radians((S + N) / 2.0))
    m = (lon >= W - 0.2) & (lon <= E + 0.2) & (lat >= S - 0.2) & (lat <= N + 0.2)
    tree = cKDTree(np.column_stack([lon[m] * cosm, lat[m]]))
    cols, rows, lons, lats = region_axes(bbox)
    TLON, TLAT = np.meshgrid(lons * cosm, lats)
    dist, idx = tree.query(np.column_stack([TLON.ravel(), TLAT.ravel()]))
    return {"mask": m, "idx": idx, "far": dist > RES * 1.6, "cols": cols, "rows": rows}

def regrid(values2d, interp):
    out = values2d[interp["mask"]].ravel()[interp["idx"]].astype("float32")
    out[interp["far"]] = np.nan
    return out

def pack(speed_kmh, dir_deg):
    s = np.where(np.isnan(speed_kmh), 255, np.clip(np.round(speed_kmh), 0, 254)).astype("uint8")
    d = np.where(np.isnan(dir_deg), 0, np.round((dir_deg % 360) * 255.0 / 360.0)).astype("uint8")
    return s.tobytes() + d.tobytes()

# ---------- R2 upload ----------
def r2_client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

def r2_put(client, key, data, content_type):
    client.put_object(Bucket=os.environ["R2_BUCKET"], Key=f"{M['prefix']}/{key}",
                      Body=gzip.compress(data), ContentType=content_type,
                      ContentEncoding="gzip", CacheControl="public, max-age=900")

# ---------- main ----------
def main():
    global M, RES
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=list(MODELS), default="continental")
    ap.add_argument("--no-upload", action="store_true", help="write to ./out instead of R2")
    ap.add_argument("--regions", default="", help="comma-separated region keys (default: all)")
    ap.add_argument("--max-hours", type=int, default=0, help="limit forecast hours (testing)")
    args = ap.parse_args()
    M = MODELS[args.model]; RES = M["res"]

    keys = [k.strip() for k in args.regions.split(",") if k.strip()] or list(REGIONS)
    hours = M["hours"][: args.max_hours] if args.max_hours else M["hours"]

    run_base, run_hh, sample = find_latest_run()
    run_dt = run_datetime(sample)
    log(f"Model {args.model} ({M['res']} deg) | latest run {run_dt.isoformat()} | {sample}")

    interps = {}
    buffers = {k: bytearray() for k in keys}
    times = []

    for hhh in hours:
        base = f"{run_base}/{run_hh}/{hhh:03d}/"
        try:
            wbuf = http_get(base + file_for(sample, M["wind_label"], hhh), binary=True)
            dbuf = http_get(base + file_for(sample, M["wdir_label"], hhh), binary=True)
        except Exception as e:
            log(f"  hour {hhh:03d}: missing ({e}); skipping"); continue

        wvals, wlat, wlon = read_grib(wbuf)
        dvals, _, _ = read_grib(dbuf)

        if not interps:
            for k in keys:
                interps[k] = build_interp(wlat, wlon, REGIONS[k])
                log(f"  region {k}: {interps[k]['cols']}x{interps[k]['rows']} cells")

        for k in keys:
            buffers[k] += pack(regrid(wvals * 3.6, interps[k]), regrid(dvals, interps[k]))
        times.append((run_dt + dt.timedelta(hours=hhh)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        log(f"  hour {hhh:03d}: ok")

    if not times:
        raise RuntimeError("No forecast hours ingested")

    manifest = {
        "model": M["name"], "run": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "res_deg": RES, "nodata": 255, "hours": times,
        "regions": {k: {"name": REGION_NAMES[k], "bbox": REGIONS[k],
                        "cols": interps[k]["cols"], "rows": interps[k]["rows"],
                        "file": f"{k}.bin"} for k in keys},
    }

    if args.no_upload:
        os.makedirs("out", exist_ok=True)
        json.dump(manifest, open("out/manifest.json", "w"), indent=2)
        for k in keys: open(f"out/{k}.bin", "wb").write(bytes(buffers[k]))
        log(f"Wrote ./out for model {args.model}")
        for k in keys:
            n = interps[k]["cols"] * interps[k]["rows"]
            spd = np.frombuffer(bytes(buffers[k][-2 * n:-n]), dtype="uint8").astype("float32")
            ok = spd[spd != 255]
            if ok.size:
                log(f"  STATS {k}: {ok.size}/{n} valid | km/h min={ok.min():.0f} mean={ok.mean():.0f} max={ok.max():.0f}")
    else:
        c = r2_client()
        r2_put(c, "manifest.json", json.dumps(manifest).encode(), "application/json")
        for k in keys:
            r2_put(c, f"{k}.bin", bytes(buffers[k]), "application/octet-stream")
        log(f"Uploaded manifest + {len(keys)} region files to R2 ({M['prefix']}/)")

if __name__ == "__main__":
    main()
