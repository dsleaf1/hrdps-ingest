#!/usr/bin/env python3
"""One-off probe: where does ECCC's Regional Deterministic Wave Prediction System (RDWPS) publish,
and does its Pacific domain have data along the inReach bot's route (Port Townsend -> Skagway)?

Writes probe_out/results.md (+ results.json). Run by .github/workflows/rdwps_probe.yml, which
pushes probe_out/ to the 'probe-results' branch so it can be read from a sandbox without Datamart.
"""
import os, re, json, math, tempfile, datetime as dt, urllib.request

ROOTS = []
now = dt.datetime.now(dt.timezone.utc)
for d in (0, 1):
    ROOTS.append(f"https://dd.weather.gc.ca/{(now - dt.timedelta(days=d)):%Y%m%d}/WXO-DD/")
ROOTS.append("https://dd.weather.gc.ca/")        # legacy (pre-2026 layout)

# Named points along the route: open coast, straits, channels and narrow passes.
POINTS = [
    ("Port Townsend", 48.12, -122.75), ("Juan de Fuca mid", 48.30, -124.00),
    ("Juan de Fuca entrance", 48.48, -124.75), ("Haro Strait", 48.55, -123.17),
    ("Active Pass", 48.875, -123.31), ("Strait of Georgia mid", 49.30, -123.70),
    ("Desolation Sound", 50.10, -124.80), ("Seymour Narrows", 50.13, -125.35),
    ("Johnstone Strait", 50.45, -126.20), ("Blackfish Sound", 50.595, -126.84),
    ("Queen Charlotte Strait", 50.80, -127.40), ("Tofino offshore", 49.10, -126.10),
    ("Brooks Peninsula", 50.05, -127.85), ("Cape Caution", 51.15, -127.80),
    ("Queen Charlotte Sound", 51.40, -129.00), ("Fitz Hugh Sound", 51.70, -127.90),
    ("Hecate Strait", 53.00, -131.00), ("Grenville Channel", 53.70, -129.80),
    ("Prince Rupert", 54.31, -130.32), ("Dixon Entrance", 54.40, -131.80),
    ("Ketchikan", 55.34, -131.65), ("Wrangell", 56.47, -132.38),
    ("Frederick Sound", 57.10, -133.80), ("Chatham Strait", 57.50, -134.80),
    ("Sitka offshore", 57.05, -135.60), ("Icy Strait", 58.25, -135.50),
    ("Lynn Canal", 58.80, -135.10), ("Skagway", 59.45, -135.32),
]

out = {"listings": {}, "candidates": [], "decoded": [], "errors": []}
md = ["# RDWPS coverage probe", f"Run at {now:%Y-%m-%d %H:%MZ}", ""]

def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "rdwps-probe"})
    with urllib.request.urlopen(req, timeout=120) as r:
        b = r.read()
    return b if binary else b.decode("utf-8", "replace")

def links(url):
    try:
        return [l for l in re.findall(r'href="([^"?][^"]*)"', get(url)) if not l.startswith(("/", "http", "../"))]
    except Exception as e:
        out["errors"].append(f"{url}: {e}")
        return None

# 1. find wave-model directories
for root in ROOTS:
    ls = links(root)
    if ls is None:
        continue
    hits = [l for l in ls if re.search(r"wps|wave|wam|vague", l, re.I)]
    out["listings"][root] = hits
    md.append(f"- `{root}` wave-like entries: {hits}")
md.append("")

# 2. walk each wave dir (bounded) to find grib2 files
def walk(url, depth, found):
    if depth > 6 or len(found) > 4000:
        return
    ls = links(url)
    if not ls:
        return
    files = [l for l in ls if l.endswith((".grib2", ".nc"))]
    if files:
        found.append((url, files))
    dirs = [l for l in ls if l.endswith("/")]
    # newest run / last hour dirs first; prune wide dirs
    if dirs and all(re.fullmatch(r"\d+/", d) for d in dirs):
        dirs = sorted(dirs)[-2:]
    for d in dirs[:12]:
        walk(url + d, depth + 1, found)

for root, hits in out["listings"].items():
    for h in hits:
        found = []
        walk(root + h, 0, found)
        for url, files in found:
            out["candidates"].append({"dir": url, "n": len(files), "sample": files[:6]})
md.append("## Directories with files")
for c in out["candidates"][:60]:
    md.append(f"- `{c['dir']}` ({c['n']} files) e.g. {c['sample'][:3]}")
md.append("")

# 2b. variables + forecast hours of the national 2.5 km system
for c in out["candidates"]:
    if "model_rdwps/national" in c["dir"]:
        fl = [l for l in (links(c["dir"]) or []) if l.endswith(".grib2")]
        labels = sorted({re.sub(r"_PT\d+H.*$", "", re.sub(r"^\d{8}T\d{2}Z_MSC_RDWPS_", "", f)) for f in fl})
        hours = sorted({int(m.group(1)) for f in fl for m in [re.search(r"_PT(\d+)H", f)] if m})
        md.append(f"## Variables in `{c['dir']}`")
        md.append(f"- {len(labels)} variables, hours {hours[:3]}...{hours[-3:]} ({len(hours)} steps)")
        md += [f"  - {l}" for l in labels]
        md.append("")
        break

# 3. decode one significant-wave-height file per Pacific-looking domain
def pick(c):
    for f in [x for x in c["sample"]] + []:
        pass
    return None

swh = []
for c in out["candidates"]:
    try:
        all_files = [l for l in (links(c["dir"]) or []) if l.endswith(".grib2")]
    except Exception:
        continue
    for f in all_files:
        if re.search(r"HTSGW|SWH|_HS_|WVHGT|HS_SFC|HTSG", f, re.I) and re.search(r"pac|nep|PNE|west|national|global", c["dir"] + f, re.I):
            swh.append(c["dir"] + f)
            break
md.append("## Wave-height files tried")
md += [f"- `{u}`" for u in swh[:8]]
md.append("")

def nearest(lat2, lon2, la, lo):
    cos = math.cos(math.radians(la))
    d2 = ((lat2 - la) ** 2 + ((lon2 - lo) * cos) ** 2)
    i = int(d2.argmin())
    return i, math.sqrt(float(d2.flat[i])) * 111.0

import numpy as np, xarray as xr
for url in swh[:4]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
            tf.write(get(url, binary=True)); path = tf.name
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        var = list(ds.data_vars)[0]
        v = np.asarray(ds[var].values, dtype="float64").squeeze()
        lat = np.asarray(ds["latitude"].values, dtype="float64")
        lon = ((np.asarray(ds["longitude"].values, dtype="float64") + 180) % 360) - 180
        if lat.ndim == 1 and v.ndim == 2:
            lon, lat = np.meshgrid(lon, lat)
        valid = np.isfinite(v)
        # grid spacing from neighbours
        dy = float(np.nanmedian(np.abs(np.diff(lat, axis=0)))) * 111.0 if lat.ndim == 2 else float("nan")
        dx = float(np.nanmedian(np.abs(np.diff(lon, axis=1)))) * 111.0 * math.cos(math.radians(52)) if lon.ndim == 2 else float("nan")
        rec = {"url": url, "var": var, "attrs": {k: str(ds[var].attrs.get(k)) for k in ("GRIB_name", "units", "GRIB_shortName")},
               "shape": list(v.shape), "lat": [float(np.nanmin(lat)), float(np.nanmax(lat))],
               "lon": [float(np.nanmin(lon)), float(np.nanmax(lon))], "spacing_km": [round(dx, 2), round(dy, 2)],
               "valid_frac": float(valid.mean()), "points": []}
        flat_lat, flat_lon, flat_v = lat.ravel(), lon.ravel(), v.ravel()
        for name, la, lo in POINTS:
            i, dkm = nearest(flat_lat, flat_lon, la, lo)
            # nearest VALID (sea) cell
            vv = np.isfinite(flat_v)
            if vv.any():
                cos = math.cos(math.radians(la))
                d2 = (flat_lat - la) ** 2 + ((flat_lon - lo) * cos) ** 2
                d2[~vv] = np.inf
                j = int(d2.argmin()); djkm = math.sqrt(float(d2[j])) * 111.0
                rec["points"].append([name, round(dkm, 1), None if not np.isfinite(flat_v[i]) else round(float(flat_v[i]), 2),
                                      round(djkm, 1), round(float(flat_v[j]), 2)])
        out["decoded"].append(rec)
        md.append(f"## `{url.rsplit('/', 1)[-1]}`")
        md.append(f"- variable {var} {rec['attrs']} shape {rec['shape']}, lat {rec['lat']}, lon {rec['lon']}, "
                  f"spacing ~{rec['spacing_km']} km, valid {rec['valid_frac']:.0%}")
        md.append("")
        md.append("| Point | nearest grid km | value there | nearest SEA cell km | value |")
        md.append("| --- | --- | --- | --- | --- |")
        for p in rec["points"]:
            md.append("| " + " | ".join(str(x) for x in p) + " |")
        md.append("")
    except Exception as e:
        out["errors"].append(f"decode {url}: {e}")

md.append("## Errors")
md += [f"- {e}" for e in out["errors"][:40]]
os.makedirs("probe_out", exist_ok=True)
open("probe_out/results.md", "w").write("\n".join(md))
json.dump(out, open("probe_out/results.json", "w"), indent=1)
print("\n".join(md))
