#!/usr/bin/env python3
"""Pre-baked wave forecasts for the wind-map PWA (phone-port plan, step 4a).

One Open-Meteo Marine multi-point call for a fixed spot list — the summary
tool's presets plus the SSCOFS virtual current stations (imported from
sscofs.py, so the map can attach waves to those markers offline) — written
to R2 as waves/latest.json. The map reads this instead of calling Open-Meteo
live from the phone, which makes waves work offline.

If Open-Meteo is unreachable, nothing is uploaded: the last good file stays
in R2 and the map ages it from the `generated` stamp.

Series format matches what the map already uses for s.waves:
  w = [[epoch_ms, Hs_m, Tp_s, dir_from_deg], ...]   (hourly, 4 days)
"""
import argparse, datetime as dt, gzip, json, os, sys, time, urllib.request

from sscofs import STATIONS as SSCOFS_STATIONS   # (region, name, lat, lon)

# Named spots from cape_st_james/hrdps_summary.html PRESETS, tagged with the
# wind map's region keys. Where a name collides with an SSCOFS station
# (Blackfish Sound, Weynton Passage), the SSCOFS coords win — those are the
# marker positions the map actually draws.
PRESETS = [
    ("haida-gwaii",            "Cape St. James",    51.9386, -131.0136),
    ("west-coast-vancouver-i", "Nahwitti Bar",      50.90,   -128.05),
    ("west-coast-vancouver-i", "Brooks Peninsula",  50.05,   -127.85),
    ("broughtons-discovery",   "Blackfish Sound",   50.595,  -126.84),
    ("broughtons-discovery",   "Weynton Passage",   50.555,  -126.80),
    ("broughtons-discovery",   "Robson Bight",      50.49,   -126.52),
    ("broughtons-discovery",   "Seymour Narrows",   50.13,   -125.35),
    ("broughtons-discovery",   "Surge Narrows",     50.235,  -125.14),
    ("northern-georgia-strait","Active Pass",       48.875,  -123.31),
    ("san-juan-gulf-islands",  "Haro Strait",       48.55,   -123.17),
]

# Haida Gwaii barotropic current-model axis points + Masset (the map's Haida
# "current stations"). Adding them here so the map can attach pre-baked waves
# to those stations by proximity → wind-against-current hazard rings work in
# Dixon/Hecate/QCS offline, not just live. Coords mirror HAIDA_TIDES in
# hrdps_map.html; names carry an index since axis points share an axis name.
HAIDA_CURRENT_POINTS = [
    ("Dixon Entrance", [(54.3,-132.6),(54.33,-131.7),(54.28,-130.9)]),
    ("Hecate Strait", [(53.55,-131.3),(53.0,-131.05),(52.35,-131.0),(51.78,-131.05),(51.6,-130.95)]),
    ("Queen Charlotte Sound", [(51.6,-129.7),(51.25,-129.0),(50.95,-128.5)]),
    ("North Graham coast", [(54.18,-132.3),(54.2,-131.7)]),
    ("West Graham coast", [(54.0,-133.25),(53.6,-133.15),(53.25,-132.95)]),
    ("West Moresby & Kunghit", [(52.7,-132.25),(52.4,-131.85),(52.15,-131.5)]),
    ("Masset Channel", [(54.0029,-132.1543)]),
]
for _axis, _pts in HAIDA_CURRENT_POINTS:
    for _i, (_la, _lo) in enumerate(_pts):
        _nm = _axis if len(_pts) == 1 else f"{_axis} {_i+1}"
        PRESETS.append(("haida-gwaii", _nm, _la, _lo))

OM_URL = ("https://marine-api.open-meteo.com/v1/marine"
          "?latitude={lats}&longitude={lons}"
          "&hourly=wave_height,wave_period,wave_direction"
          "&forecast_days=4&timezone=GMT")

def log(*a): print(*a, file=sys.stderr, flush=True)

def spot_list():
    spots = {}                                   # name -> (region, name, lat, lon); later wins
    for region, name, lat, lon in PRESETS:
        spots[name] = (region, name, lat, lon)
    for region, name, lat, lon in SSCOFS_STATIONS:
        spots[name] = (region, name, lat, lon)
    return list(spots.values())

def fetch_marine(spots):
    url = OM_URL.format(lats=",".join(f"{s[2]:.4f}" for s in spots),
                        lons=",".join(f"{s[3]:.4f}" for s in spots))
    last = None
    for attempt in range(3):
        if attempt: time.sleep(20 * attempt)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.loads(r.read())
            if isinstance(data, dict): data = [data]  # single-point responses aren't wrapped
            if len(data) != len(spots):
                raise ValueError(f"expected {len(spots)} points, got {len(data)}")
            return data
        except Exception as e:
            last = e; log(f"Open-Meteo attempt {attempt+1}/3 failed: {e}")
    raise RuntimeError(f"Open-Meteo Marine unreachable: {last}")

def build(spots, data, now_iso):
    out = []
    for (region, name, lat, lon), point in zip(spots, data):
        h = (point or {}).get("hourly") or {}
        times = h.get("time") or []
        hs, tp, dr = h.get("wave_height"), h.get("wave_period"), h.get("wave_direction")
        rec = {"name": name, "region": region, "lat": round(lat, 4), "lon": round(lon, 4)}
        if times and hs and any(v is not None for v in hs):
            w = []
            for i, ts in enumerate(times):
                ms = int(dt.datetime.fromisoformat(ts + ":00+00:00").timestamp() * 1000)
                w.append([ms,
                          None if hs[i] is None else round(hs[i], 2),
                          None if tp[i] is None else round(tp[i], 1),
                          None if dr[i] is None else int(round(dr[i]))])
            rec["w"] = w
        else:
            rec["nodata"] = True                 # out of the marine grid (tight channels) — map skips it
        out.append(rec)
    n_ok = sum(1 for r in out if "w" in r)
    log(f"{n_ok}/{len(out)} spots with wave data")
    if n_ok == 0:
        raise RuntimeError("no spot returned wave data — refusing to overwrite last good file")
    return {"generated": now_iso, "source": "Open-Meteo Marine (best_match)", "spots": out}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spots = spot_list()
    log(f"{len(spots)} spots")
    try:
        doc = build(spots, fetch_marine(spots), now_iso)
    except Exception as e:
        # Keep the last good waves/latest.json in R2; the map shows its age.
        log(f"SKIP (keeping last good file): {e}")
        return

    body = json.dumps(doc, separators=(",", ":")).encode()
    if a.no_upload:
        open("waves_latest.json", "wb").write(body)
        log(f"wrote ./waves_latest.json ({len(body)//1024} KB)")
        return

    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    s3.put_object(Bucket=os.environ["R2_BUCKET"], Key="waves/latest.json",
                  Body=gzip.compress(body), ContentType="application/json",
                  ContentEncoding="gzip", CacheControl="max-age=600")
    log(f"uploaded waves/latest.json ({len(body)//1024} KB raw)")

if __name__ == "__main__":
    main()
