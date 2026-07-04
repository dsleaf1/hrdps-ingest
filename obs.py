#!/usr/bin/env python3
"""
Marine observation collector for the verification strip in hrdps_summary.html.

Sources (server-side because neither sends CORS headers for browsers):
  - ECCC Datamart SWOB-ML moored buoys: all 46xxx buoys reporting today
    (dd.weather.gc.ca/{date}/WXO-DD/observations/swob-ml/marine/moored-buoys/{date}/<msc_id>/)
  - Cape St. James CS land station via api.weather.gc.ca swob-realtime
    (bbox+datetime query; NOTE: adding &sortby silently returns 0 features — sort client-side)

Output: obs/latest.json in the R2 bucket (browser-readable, CORS *):
  { "generated": iso, "stations": [ {id,name,kind,lat,lon,time,wspd_kt,gust_kt,wdir,
                                     hs_m,tp_s,mslp,atemp}, ... ] }
Wind converted km/h -> knots here. Buoy anemometers sit ~5 m (vs 10 m model level).

Self-contained: stdlib + curl only (no numpy/xarray), boto3 only for upload.
Local test:  python obs.py --no-upload
"""
import os, re, sys, json, argparse, subprocess, gzip
import datetime as dt

DATAMART = "https://dd.weather.gc.ca"
API = "https://api.weather.gc.ca"

# land stations to include via swob-realtime bbox lookup: (id, name, bbox W,S,E,N)
LAND = [
    ("csj", "Cape St. James CS", (-131.2, 51.8, -130.8, 52.1)),
]

def log(*a): print(*a, file=sys.stderr, flush=True)

def curl(url, timeout=60):
    r = subprocess.run(["curl", "-gsS", "-m", str(timeout), "--retry", "2", url],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"curl {url}: {r.stderr.decode()[:200]}")
    return r.stdout

def links(url):
    return re.findall(r'href="([^"?][^"]*)"', curl(url).decode("utf-8", "replace"))

def num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None

def parse_swob_xml(xml):
    """SWOB-ML <element name=".." uom=".." value=".."> pairs -> dict."""
    out = {}
    for m in re.finditer(r'<element[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"', xml):
        out.setdefault(m.group(1), m.group(2))
    return out

def kmh_to_kt(v):
    return None if v is None else round(v / 1.852, 1)

def buoy_stations(dates):
    """Fetch the newest SWOB file for every 46xxx buoy reporting on the given dates."""
    out = {}
    for date in dates:                       # today first, then yesterday for stragglers
        base = f"{DATAMART}/{date}/WXO-DD/observations/swob-ml/marine/moored-buoys/{date}"
        try:
            ids = sorted({l.strip("/") for l in links(base + "/") if re.fullmatch(r"46\d{5}/", l)})
        except Exception as e:
            log(f"buoy list {date}: {e}"); continue
        for msc in ids:
            wmo = "46" + msc[-3:]
            if wmo in out: continue          # already have a newer date's obs
            try:
                files = sorted(f for f in links(f"{base}/{msc}/") if f.endswith("swob.xml"))
                if not files: continue
                e = parse_swob_xml(curl(f"{base}/{msc}/{files[-1]}").decode("utf-8", "replace"))
            except Exception as ex:
                log(f"buoy {wmo}: {ex}"); continue
            out[wmo] = {
                "id": wmo, "name": (e.get("stn_nam") or wmo).title(), "kind": "buoy",
                "lat": num(e.get("lat")), "lon": num(e.get("long")),
                "time": e.get("date_tm"),
                "wspd_kt": kmh_to_kt(num(e.get("avg_wnd_spd_pst10mts"))),
                "gust_kt": kmh_to_kt(num(e.get("max_wnd_spd_pst10mts"))),
                "wdir": num(e.get("avg_wnd_dir_pst10mts")),
                "hs_m": num(e.get("avg_sig_wave_hgt_pst20mts")),
                "tp_s": num(e.get("pk_wave_pd_pst20mts")) or num(e.get("avg_sig_wave_pd_pst20mts")),
                "mslp": num(e.get("avg_stn_pres_pst10mts")),   # station level ~sea level on a buoy
                "atemp": num(e.get("avg_air_temp_pst10mts")),
            }
            log(f"buoy {wmo} {out[wmo]['name']}: {out[wmo]['wspd_kt']} kt @ {out[wmo]['time']}")
    return list(out.values())

def land_stations(now):
    out = []
    since = (now - dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sid, name, (w, s, e, n) in LAND:
        url = (f"{API}/collections/swob-realtime/items?f=json&limit=20"
               f"&bbox={w},{s},{e},{n}&datetime={since}%2F..")
        try:
            feats = json.loads(curl(url))["features"]
        except Exception as ex:
            log(f"land {sid}: {ex}"); continue
        if not feats: log(f"land {sid}: no recent obs"); continue
        f = max(feats, key=lambda f: f["properties"].get("date_tm-value", ""))
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        out.append({
            "id": sid, "name": name, "kind": "land",
            "lat": lat, "lon": lon,
            "time": p.get("date_tm-value"),
            "wspd_kt": kmh_to_kt(num(p.get("avg_wnd_spd_10m_pst1hr")) or num(p.get("avg_wnd_spd_10m_pst1mt"))),
            "gust_kt": kmh_to_kt(num(p.get("max_wnd_spd_10m_pst1hr")) or num(p.get("max_wnd_spd_10m_pst1mt"))),
            "wdir": num(p.get("avg_wnd_dir_10m_pst1hr")) or num(p.get("avg_wnd_dir_10m_pst1mt")),
            "hs_m": None, "tp_s": None,
            "mslp": num(p.get("mslp")),
            "atemp": num(p.get("air_temp")),
        })
        log(f"land {sid}: {out[-1]['wspd_kt']} kt @ {out[-1]['time']}")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    dates = [now.strftime("%Y%m%d"), (now - dt.timedelta(days=1)).strftime("%Y%m%d")]
    stations = buoy_stations(dates) + land_stations(now)
    if not stations:
        raise RuntimeError("no observations collected")
    doc = {"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": stations}
    body = json.dumps(doc).encode()
    log(f"{len(stations)} stations, {len(body)} bytes")

    if a.no_upload:
        json.dump(doc, open("obs_latest.json", "w"), indent=1)
        log("wrote ./obs_latest.json")
        return
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    s3.put_object(Bucket=os.environ["R2_BUCKET"], Key="obs/latest.json",
                  Body=gzip.compress(body), ContentType="application/json",
                  ContentEncoding="gzip", CacheControl="public, max-age=300")
    log("uploaded obs/latest.json")

if __name__ == "__main__":
    main()
