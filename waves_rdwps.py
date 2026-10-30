#!/usr/bin/env python3
"""
RDWPS wave bake for the inReach bot's `wv` command.

ECCC's Regional Deterministic Wave Prediction System, national 2.5 km (rotated grid RLatLon0.0225,
the same grid as HRDPS), 4 runs/day, hourly to 48 h. Fields:
  HTSGW    significant height of combined wind waves and swell (m)
  PWPER    primary wave mean period (s)
  PWAVEDIR primary wave direction, degrees the waves come FROM

Regridded nearest-neighbour onto exactly the bot's HRDPS region grids (ingest.py bbox_of/res_of),
so a cell index means the same place in both bakes. Land / outside the model = 255.

Output (R2, prefix rdwps/): manifest.json + {region}.bin, per hour: rows*cols height bytes
(decimetres, 0..254), then period bytes (whole seconds), then direction bytes (0..360 -> 0..255).
Hour offset = h * 3 * rows * cols.

Local smoke test:  python waves_rdwps.py --no-upload --max-hours 2
"""
import os, re, sys, json, gzip, argparse, datetime as dt
import numpy as np
import ingest as I                      # http_get, list_links, read_grib, build_interp, regrid, bbox_of, res_of, r2_client

BASE = "https://dd.weather.gc.ca/{date}/WXO-DD/model_rdwps/national/2.5km"
REGIONS = ["coast-overview", "north-passage"]   # the inReach bot's regions
FIELDS = ["HTSGW", "PWPER", "PWAVEDIR"]
HOURS = list(range(0, 49))
PREFIX = "rdwps"

def log(*a): print(*a, file=sys.stderr, flush=True)

def find_latest_run():
    now = dt.datetime.now(dt.timezone.utc)
    for d in (0, 1):
        b = BASE.format(date=(now - dt.timedelta(days=d)).strftime("%Y%m%d"))
        try:
            runs = sorted({l.strip("/") for l in I.list_links(b + "/") if re.fullmatch(r"\d{2}/", l)}, reverse=True)
        except Exception:
            continue
        for run in runs:
            try:
                files = I.list_links(f"{b}/{run}/")
            except Exception:
                continue
            last = [f for f in files if "_HTSGW_" in f and f.endswith("_PT048H.grib2")]
            if last:
                return b, run, last[0]
    raise RuntimeError("no complete RDWPS run found")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--max-hours", type=int, default=0)
    args = ap.parse_args()
    hours = HOURS[: args.max_hours] if args.max_hours else HOURS

    base, run, sample = find_latest_run()
    m = re.search(r"(\d{8})T(\d{2})Z", sample)
    run_dt = dt.datetime.strptime(m.group(1), "%Y%m%d").replace(hour=int(m.group(2)), tzinfo=dt.timezone.utc)
    log(f"RDWPS run {run_dt:%Y-%m-%dT%HZ} | {sample}")

    interps, bufs, times = {}, {k: bytearray() for k in REGIONS}, []
    for h in hours:
        vals = {}
        try:
            for f in FIELDS:
                name = sample.replace("_HTSGW_", f"_{f}_").replace("_PT048H", f"_PT{h:03d}H")
                v, lat, lon = I.read_grib(I.http_get(f"{base}/{run}/{name}", binary=True))
                vals[f] = v
        except Exception as e:
            log(f"  hour {h:03d}: missing ({e}); skipping"); continue
        if not interps:
            for k in REGIONS:
                interps[k] = I.build_interp(lat, lon, I.bbox_of(k), I.res_of(k))
                log(f"  region {k}: {interps[k]['cols']}x{interps[k]['rows']}")
        for k in REGIONS:
            hs = I.regrid(vals["HTSGW"], interps[k])
            tp = I.regrid(vals["PWPER"], interps[k])
            dr = I.regrid(vals["PWAVEDIR"], interps[k])
            bad = ~np.isfinite(hs) | (hs > 25.4) | (hs < 0)
            hb = np.where(bad, 255, np.clip(np.round(hs * 10), 0, 254)).astype("uint8")
            tb = np.where(bad | ~np.isfinite(tp), 255, np.clip(np.round(tp), 0, 254)).astype("uint8")
            db = np.where(bad | ~np.isfinite(dr), 255, np.round((dr % 360) * 255.0 / 360.0) % 255).astype("uint8")
            bufs[k] += hb.tobytes() + tb.tobytes() + db.tobytes()
        times.append((run_dt + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        log(f"  hour {h:03d}: ok")
    if not times:
        raise RuntimeError("no hours ingested")

    manifest = {
        "model": "ECCC RDWPS national 2.5 km", "run": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": times, "nodata": 255,
        "encoding": {"height": "decimetres", "period": "seconds", "direction": "from, 0..360 -> 0..255",
                     "layout": "per hour: height[rows*cols], period[rows*cols], direction[rows*cols]"},
        "regions": {k: {"bbox": I.bbox_of(k), "cols": interps[k]["cols"], "rows": interps[k]["rows"],
                        "res_deg": I.res_of(k), "file": f"{k}.bin"} for k in REGIONS},
    }

    # coverage summary (GitHub annotations in CI)
    for k in REGIONS:
        n = interps[k]["cols"] * interps[k]["rows"]
        hb = np.frombuffer(bytes(bufs[k][:n]), dtype="uint8")
        ok = hb[hb != 255]
        msg = (f"{k}: {ok.size}/{n} sea cells ({ok.size / n:.0%}), h0 height max {ok.max() / 10 if ok.size else '-'} m, "
               f"{len(times)} h, gz {len(gzip.compress(bytes(bufs[k]))) / 1e6:.1f} MB")
        log(msg)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=rdwps::{msg}", flush=True)

    if args.no_upload:
        os.makedirs("out", exist_ok=True)
        json.dump(manifest, open("out/rdwps_manifest.json", "w"), indent=1)
        for k in REGIONS:
            open(f"out/rdwps_{k}.bin", "wb").write(bytes(bufs[k]))
        log("wrote ./out")
        return
    c = I.r2_client()
    bucket = os.environ["R2_BUCKET"]
    def put(key, data, ct):
        c.put_object(Bucket=bucket, Key=f"{PREFIX}/{key}", Body=gzip.compress(data), ContentType=ct,
                     ContentEncoding="gzip", CacheControl="public, max-age=900")
    for k in REGIONS:
        put(f"{k}.bin", bytes(bufs[k]), "application/octet-stream")
    put("manifest.json", json.dumps(manifest).encode(), "application/json")   # manifest last
    log(f"uploaded {PREFIX}/ ({len(REGIONS)} regions)")

if __name__ == "__main__":
    main()
