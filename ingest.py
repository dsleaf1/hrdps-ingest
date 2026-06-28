#!/usr/bin/env python3
"""
HRDPS 2.5 km wind ingest for the BC-coast kayaker map.

Pipeline:
  1. Find the latest complete HRDPS Continental run on ECCC Datamart.
  2. For each forecast hour, download 10 m WIND (speed) + WDIR (direction) GRIB2
     (native rotated ~2.5 km grid, code RLatLon0.0225).
  3. Regrid each kayak region to a regular lat/lon grid at ~0.0225 deg (~2.5 km),
     nearest-neighbour from the rotated grid.
  4. Byte-pack speed (km/h, 255 = nodata) + direction (0..360 -> 0..255) per hour.
  5. gzip + upload per-region binaries and a manifest.json to Cloudflare R2.

Runs on GitHub Actions 4x/day. Local smoke test (needs eccodes + deps):
  python ingest.py --no-upload --regions haida-gwaii --max-hours 3
"""
import os, re, sys, gzip, json, time, argparse, tempfile
import datetime as dt
import urllib.request
import numpy as np

DATAMART = "https://dd.weather.gc.ca/today/model_hrdps/continental/2.5km"
RES = 0.0225                         # target grid spacing (deg), ~2.5 km
FCST_HOURS = list(range(0, 49))      # 000..048

# region key -> bbox [lonW, latS, lonE, latN]  (must match the map app's REGIONS)
REGIONS = {
    "haida-gwaii":             [-132.8, 51.4, -130.6, 53.6],
    "west-coast-vancouver-i":  [-128.7, 48.3, -124.8, 50.9],
    "broughtons-discovery":    [-127.3, 49.9, -124.7, 50.95],
    "northern-georgia-strait": [-125.4, 49.0, -123.4, 50.2],
    "san-juan-gulf-islands":   [-123.7, 48.3, -122.5, 49.3],
    "puget-sound":             [-123.3, 47.0, -122.2, 48.4],
}
REGION_NAMES = {
    "haida-gwaii": "Haida Gwaii",
    "west-coast-vancouver-i": "West Coast Vancouver I.",
    "broughtons-discovery": "Broughtons & Discovery",
    "northern-georgia-strait": "Northern Georgia Strait",
    "san-juan-gulf-islands": "San Juan & Gulf Islands",
    "puget-sound": "Puget Sound",
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

def find_latest_run():
    """Return (run_hh, sample_wind_filename) for the newest run whose hour 048 exists."""
    runs = sorted({l.strip("/") for l in list_links(DATAMART + "/") if re.fullmatch(r"\d{2}/", l)}, reverse=True)
    for run in runs:
        try:
            files = list_links(f"{DATAMART}/{run}/048/")
        except Exception:
            continue
        wind = [f for f in files if "_WIND_AGL-10m_" in f and f.endswith(".grib2")]
        if wind:
            return run, wind[0]
    raise RuntimeError("No complete HRDPS run found on Datamart")

def run_datetime(sample_filename, run_hh):
    # filename like 20260628T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT048H.grib2
    m = re.match(r"(\d{8})T(\d{2})Z", sample_filename)
    d = dt.datetime.strptime(m.group(1), "%Y%m%d").replace(
        hour=int(m.group(2)), tzinfo=dt.timezone.utc)
    return d

def hour_filename(sample, var, hhh):
    f = re.sub(r"_(WIND|WDIR)_", f"_{var}_", sample)
    f = re.sub(r"PT\d{3}H", f"PT{hhh:03d}H", f)
    return f

# ---------- GRIB decode ----------
def read_grib(buf):
    """Return (values2d, lat2d, lon2d[-180..180]) from a single-field GRIB2 message."""
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
    lons = np.linspace(W, E, cols)
    lats = np.linspace(N, S, rows)          # north -> south (row 0 = north)
    return cols, rows, lons, lats

def build_interp(lat, lon, bbox):
    """KDTree over source points near the region (lon scaled by cos(midlat))."""
    from scipy.spatial import cKDTree
    W, S, E, N = bbox
    midlat = (S + N) / 2.0
    cosm = np.cos(np.radians(midlat))
    m = (lon >= W - 0.2) & (lon <= E + 0.2) & (lat >= S - 0.2) & (lat <= N + 0.2)
    tree = cKDTree(np.column_stack([lon[m] * cosm, lat[m]]))
    cols, rows, lons, lats = region_axes(bbox)
    TLON, TLAT = np.meshgrid(lons * cosm, lats)
    dist, idx = tree.query(np.column_stack([TLON.ravel(), TLAT.ravel()]))
    far = dist > RES * 1.6
    return {"mask": m, "idx": idx, "far": far, "cols": cols, "rows": rows}

def regrid(values2d, interp):
    src = values2d[interp["mask"]].ravel()
    out = src[interp["idx"]].astype("float32")
    out[interp["far"]] = np.nan
    return out  # flat, length rows*cols, row 0 = north

def pack(speed_kmh, dir_deg):
    s = np.where(np.isnan(speed_kmh), 255, np.clip(np.round(speed_kmh), 0, 254)).astype("uint8")
    d = np.where(np.isnan(dir_deg), 0, np.round((dir_deg % 360) * 255.0 / 360.0)).astype("uint8")
    return s.tobytes() + d.tobytes()

# ---------- R2 upload ----------
def r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )

def r2_put(client, key, data, content_type):
    client.put_object(
        Bucket=os.environ["R2_BUCKET"], Key="hrdps/" + key,
        Body=gzip.compress(data), ContentType=content_type,
        ContentEncoding="gzip", CacheControl="public, max-age=900",
    )

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true", help="write to ./out instead of R2")
    ap.add_argument("--regions", default="", help="comma-separated region keys (default: all)")
    ap.add_argument("--max-hours", type=int, default=0, help="limit forecast hours (testing)")
    args = ap.parse_args()

    keys = [k.strip() for k in args.regions.split(",") if k.strip()] or list(REGIONS)
    hours = FCST_HOURS[: args.max_hours] if args.max_hours else FCST_HOURS

    run_hh, sample = find_latest_run()
    run_dt = run_datetime(sample, run_hh)
    log(f"Latest run: {run_dt.isoformat()}  ({sample})")

    interps = {}                         # built once (grid geometry is constant)
    buffers = {k: bytearray() for k in keys}
    times = []

    for hhh in hours:
        wname = hour_filename(sample, "WIND", hhh)
        dname = hour_filename(sample, "WDIR", hhh)
        base = f"{DATAMART}/{run_hh}/{hhh:03d}/"
        try:
            wbuf = http_get(base + wname, binary=True)
            dbuf = http_get(base + dname, binary=True)
        except Exception as e:
            log(f"  hour {hhh:03d}: missing ({e}); skipping"); continue

        wvals, wlat, wlon = read_grib(wbuf)
        dvals, _, _ = read_grib(dbuf)

        if not interps:
            for k in keys:
                interps[k] = build_interp(wlat, wlon, REGIONS[k])
                log(f"  region {k}: {interps[k]['cols']}x{interps[k]['rows']} cells")

        for k in keys:
            spd = regrid(wvals * 3.6, interps[k])     # m/s -> km/h
            drc = regrid(dvals, interps[k])
            buffers[k] += pack(spd, drc)

        times.append((run_dt + dt.timedelta(hours=hhh)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        log(f"  hour {hhh:03d}: ok")

    if not times:
        raise RuntimeError("No forecast hours ingested")

    manifest = {
        "model": "HRDPS Continental 2.5 km (ECCC)",
        "run": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "res_deg": RES, "nodata": 255, "hours": times,
        "regions": {
            k: {"name": REGION_NAMES[k], "bbox": REGIONS[k],
                "cols": interps[k]["cols"], "rows": interps[k]["rows"],
                "file": f"{k}.bin"} for k in keys
        },
    }

    if args.no_upload:
        os.makedirs("out", exist_ok=True)
        json.dump(manifest, open("out/manifest.json", "w"), indent=2)
        for k in keys: open(f"out/{k}.bin", "wb").write(bytes(buffers[k]))
        log("Wrote ./out (manifest.json + region .bin files)")
    else:
        c = r2_client()
        r2_put(c, "manifest.json", json.dumps(manifest).encode(), "application/json")
        for k in keys:
            r2_put(c, f"{k}.bin", bytes(buffers[k]), "application/octet-stream")
        log(f"Uploaded manifest + {len(keys)} region files to R2 (hrdps/)")

if __name__ == "__main__":
    main()
