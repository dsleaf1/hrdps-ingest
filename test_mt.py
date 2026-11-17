#!/usr/bin/env python3
"""Offline tests for the mountain forecast: mountain.py maths and the bot's `mt` command, against
synthetic data (no network, no GRIB). Run: python test_mt.py — prints PASS/FAIL per case."""
import json, math, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np
import mountain as M
import inreach_bot as B

UTC = dt.timezone.utc
PT = ZoneInfo("America/Vancouver")
FAILS = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# ------------------------------------------------------------------ mountain.py maths
run = dt.datetime(2026, 11, 16, 18, tzinfo=UTC)                 # 18Z = 11:00 Monday in BC (UTC-7 all year from 2026)
steps = M.plan_steps(run)
loc = [(t.astimezone(PT).strftime("%a%H"), fh, w) for t, fh, w in steps]
check("plan: 18Z run covers Mon 12-20, all Tue, Wed 04-08",
      [x[0] for x in loc] == ["Mon12", "Mon14", "Mon16", "Mon20", "Tue04", "Tue08", "Tue12", "Tue14",
                              "Tue16", "Tue20", "Wed04", "Wed08"], str(loc))
check("plan: forecast hours", [t.hour for t, fh, w in steps] == [(h + 7) % 24 for h in (12, 14, 16, 20, 4, 8, 12, 14, 16, 20, 4, 8)]
      and all(run + dt.timedelta(hours=fh) == t for t, fh, w in steps))
check("plan: 14:00 steps are freezing-level only", [x[2] for x in loc if x[0].endswith("14")] == [False, False])
for hh in (0, 6, 12, 18):                                       # every run reaches all of the next local day
    r = dt.datetime(2026, 11, 16, hh, tzinfo=UTC)
    nextday = (r + dt.timedelta(hours=5)).astimezone(PT).date() + dt.timedelta(days=1)
    got = {t.astimezone(PT).hour for t, fh, w in M.plan_steps(r) if t.astimezone(PT).date() == nextday and w}
    check(f"plan: {hh:02d}Z run holds every slot of the following day", got == set(M.SLOTS), str(got))
summer = M.plan_steps(dt.datetime(2026, 7, 10, 12, tzinfo=UTC))
check("plan: summer run keeps local 04 08 12 16 20",
      {t.astimezone(PT).hour for t, fh, w in summer if w} == set(M.SLOTS))

H = np.array([[1000.0], [2000.0], [3000.0]])
s, d = M.to_heights(H, np.array([[10.0], [20.0], [40.0]]), np.array([[270.0], [270.0], [270.0]]), [1500, 2000, 2500, 3500])
check("heights: linear between levels", np.allclose(s[:3, 0], [15, 20, 30]) and np.allclose(d[:3, 0], 270))
check("heights: NaN above the top level", np.isnan(s[3, 0]))
s, d = M.to_heights(H, np.array([[10.0], [10.0], [10.0]]), np.array([[350.0], [10.0], [10.0]]), [1500])
check("heights: direction interpolates through north", abs(((d[0, 0] + 180) % 360) - 180) < 1e-3, str(d))
s, d = M.to_heights(H, np.array([[10.0], [10.0], [10.0]]), np.array([[90.0], [270.0], [270.0]]), [1500])
check("heights: opposing winds cancel instead of averaging speeds", s[0, 0] < 1e-3)

Hc = np.array([[100.0] * 4, [1000.0] * 4, [2000.0] * 4, [3000.0] * 4])
Tc = np.array([[6, -1, -5, 8], [2, 3, -8, 6], [-4, -2, -12, 4], [-10, -8, -20, 2]], dtype="float32")
fz = M.freezing_level(Hc, Tc)
check("freezing: simple crossing", abs(fz[0] - (1000 + 2 / 6 * 1000)) < 1)
check("freezing: inversion takes the highest crossing", abs(fz[1] - 1600) < 1, str(fz[1]))
check("freezing: whole column frozen -> 0", fz[2] == 0)
check("freezing: whole column warm -> above top", np.isinf(fz[3]))

mm = np.array([0, 0.04, 0.3, 0.5, 4.2, 10, 12.4, 37, 200, np.nan])
dec = [None if b == 255 else b / 10 if b <= 100 else b - 90 for b in M.enc_precip(mm)]
check("precip: encode/decode", dec[:8] == [0, 0, 0.3, 0.5, 4.2, 10, 12, 37] and dec[8] == 164 and dec[9] is None, str(dec))

# ------------------------------------------------------------------ synthetic bake -> bot
# A 3x2-cell region. Cell 0 terrain 1769 m (Mt Olsen's model height); cell 1 terrain 2600 m.
NC = 6
LEVH = np.array([110, 540, 990, 1460, 1950, 2470, 3010, 3590, 4210, 4880], dtype="float32")
terrain = np.array([1769, 2600, 0, 0, 0, 0], dtype="float32")

def bake(run, wind_fn, temp_fn, precip_fn):
    """wind_fn(local_dt) -> (m/s, deg) at 2000 m (speed grows in proportion to height); temp_fn -> sea-level C;
    precip_fn -> mm."""
    cols, times, wind = [], [], []
    for t, fh, w in M.plan_steps(run):
        lt = t.astimezone(PT)
        Hs = np.repeat(LEVH[:, None], NC, axis=1)
        T = temp_fn(lt) - 6.5e-3 * Hs
        if w:
            sp, dr = wind_fn(lt)
            S = sp * Hs / 2000                                # linear in height: 1x at 2000 m, 1.5x at 3000 m
            D = np.full_like(Hs, dr)
        cols += M.step_columns(Hs, T, S if w else None, D if w else None,
                               np.full(NC, precip_fn(lt)) if (w and fh >= M.PRECIP_H) else None)
        times.append(t.strftime("%Y-%m-%dT%H:%M:%SZ")); wind.append(w)
    rec = np.stack([M.enc_scaled(terrain, M.TERRAIN_M)] + cols, axis=1)
    step_len = 2 * len(M.HEIGHTS) + 2
    man = {"run": run.strftime("%Y-%m-%dT%H:%M:%SZ"), "steps": times, "wind": wind, "heights_m": M.HEIGHTS,
           "tz": M.TZ, "steps_local": [dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(PT).strftime("%Y-%m-%dT%H") for t in times],
           "step_len": step_len, "record_len": 1 + len(times) * step_len, "precip_hours": M.PRECIP_H,
           "regions": {"t": {"bbox": [-124.0, 51.0, -123.9, 51.25], "cols": 3, "rows": 2, "file": "t.bin"}}}
    blob = rec.tobytes()
    calls = []
    def ranger(url, start, length):
        calls.append((url, start, length)); return blob[start:start + length]
    mt = B.Mountain(base="x", loader=lambda url: json.dumps(man).encode(), ranger=ranger)
    return mt, calls

def tue_wind(lt):       # km/h at 2000 m: 14 16 18 20 22 through Tuesday's slots, from SW
    return ((10 + lt.hour / 2 + (4 if lt.hour >= 4 else 0)) / 3.6, 225.0)
def temp(lt):           # freezing level peaks at 14:00: 1200 m at 14h, lower either side
    return 6.5e-3 * (1200 - 60 * abs(lt.hour - 14))
def rain(lt):
    return {4: 0.0, 8: 0.2, 12: 3.4, 16: 12.0, 20: 0.0}[lt.hour]

LAT, LON = 51.25, -124.0                                        # cell 0
mt18, calls = bake(dt.datetime(2026, 11, 16, 18, tzinfo=UTC), tue_wind, temp, rain)
five_pm = dt.datetime(2026, 11, 16, 17, 0, tzinfo=PT)
out = B.build_mt_reply(mt18, LAT, LON, 17, now=five_pm)
print(out[0]); print(f"({len(out[0])} chars)")
L = out[0].split("\n")
check("5 pm the day before: one message, 3 header lines + 5 slots", len(out) == 1 and len(L) == 8)
check("line 1: point and run, as wx", L[0] == "51.25,-124.00 18Z", L[0])
check("line 2: day and freezing-level max from the 14:00 sample", L[1] == "Tu17 FZmax1200", L[1])
check("line 3: elevations", L[2] == "2000/3000m", L[2])
check("slots are 04 08 12 16 20", [l[:2] for l in L[3:]] == ["04", "08", "12", "16", "20"])
check("wind: 2000 m and 3000 m, km/h, from SW", L[3].split()[1] == "SW16/SW24" and L[7].split()[1] == "SW24/SW36", L[3] + " | " + L[7])
check("precip: 0, trace, whole mm", [l.split()[2] for l in L[3:]] == ["0", "T", "3", "12", "0"], str(L[3:]))
check("no line wraps on a 20-character screen", max(len(l) for l in L) <= B.MT_LINE, str([len(l) for l in L]))
check("fits one inReach message", len(out[0]) <= 160)
check("one Range read of one record", len(calls) == 1 and calls[0][1] == 0 and calls[0][2] == mt18.rec_len)

mt06, _ = bake(dt.datetime(2026, 11, 17, 6, tzinfo=UTC), tue_wind, temp, rain)   # the run held at 4:30 am
out = B.build_mt_reply(mt06, LAT, LON, 17, now=dt.datetime(2026, 11, 17, 4, 30, tzinfo=PT))
L = out[0].split("\n")
check("4:30 am on the day: still all five slots incl. 04", [l[:2] for l in L[3:]] == ["04", "08", "12", "16", "20"], out[0])
check("4:30 am on the day: 06Z run in header", L[0].endswith(" 06Z") and L[1].startswith("Tu17 "))

out = B.build_mt_reply(mt06, LAT, LON, 17, now=dt.datetime(2026, 11, 17, 13, 10, tzinfo=PT))
L = out[0].split("\n")
check("today at 13:10: most recent slot and those ahead", [l[:2] for l in L[3:]] == ["12", "16", "20"], out[0])
check("today at 13:10: freezing max still sees 14:00", "FZmax1200" in L[1], L[1])
out = B.build_mt_reply(mt06, LAT, LON, 17, now=dt.datetime(2026, 11, 17, 18, 0, tzinfo=PT))
check("today at 18:00: freezing max only over what is left", "FZmax1100" in out[0], out[0])

out = B.build_mt_reply(mt18, LAT, LON, 18, now=five_pm)
check("day after tomorrow is refused, naming the days available",
      out == ["No mt forecast for day 18: HRDPS reaches 48 h. Available: Mo16 Tu17"], out[0])
out = B.build_mt_reply(mt18, LAT, LON, 15, now=five_pm)
check("yesterday's date is refused", out[0].startswith("No mt forecast for day 15"), out[0])
check("refusals fit one message", len(out[0]) <= 160)

out = B.build_mt_reply(mt18, LAT, LON, 17, [1500, 2500, 3500], now=five_pm)
L = out[0].split("\n"); print(out[0]); print(f"({len(out[0])} chars)")
check("custom elevations: below model terrain shows '-' and the ground height",
      L[1] == "Tu17 FZmax1200" and L[2] == "1500/2500/3500m gnd1760" and L[3].split()[1].startswith("-/SW"), L[2] + " | " + L[3])
check("custom elevations: interpolated between 500 m heights", L[3].split()[1] == "-/SW20/SW28", L[3])
check("three elevations fit one message", len(out[0]) <= 160)

out = B.build_mt_reply(mt18, 51.25, -123.95, 17, now=five_pm)   # cell 1: terrain 2600 m
check("default 2000 m below terrain at a high cell", out[0].split("\n")[3].split()[1].startswith("-/SW") and out[0].split("\n")[2] == "2000/3000m gnd2600", out[0])
check("outside the region", B.build_mt_reply(mt18, 49.0, -120.0, 17, now=five_pm)[0].startswith("49.00,-120.00 is outside the mt forecast area"))

gale, _ = bake(dt.datetime(2026, 11, 16, 18, tzinfo=UTC), lambda lt: (30.0, 247.5), temp, lambda lt: 25.0)
out = B.build_mt_reply(gale, LAT, LON, 17, [2000, 3000, 4000], now=five_pm)
print(out[0]); print(f"({len(out[0])} chars)")
check("worst case (3 elevations, 3-digit winds, 2-digit rain) is cut to <= 160", len(out[0]) <= 160, str(len(out[0])))
check("worst case keeps the earliest slots", out[0].split("\n")[3].startswith("04 "))

breeze, _ = bake(dt.datetime(2026, 11, 16, 18, tzinfo=UTC), lambda lt: (9.0, 247.5), temp, lambda lt: 14.0)
out = B.build_mt_reply(breeze, LAT, LON, 17, [2000, 3000, 4000], now=five_pm)
print(out[0]); print(f"({len(out[0])} chars)")
check("a slot line over 20 characters falls back to the 8-point compass",
      "WSW" not in out[0] and max(len(l) for l in out[0].split("\n")[3:]) <= B.MT_LINE, out[0])

lost, _ = bake(dt.datetime(2026, 11, 16, 0, tzinfo=UTC), tue_wind, temp, rain)   # stale run: ends Tue 16:00
out = B.build_mt_reply(lost, LAT, LON, 17, now=five_pm)
check("missed ingest: the slot beyond the run shows '-'", out[0].split("\n")[-1] == "20 -", out[0])

# ------------------------------------------------------------------ request parsing
GARMIN = "\n\nView the location or send a reply to Dave: https://inreachlink.com/ABC123\n\nDave sent this message from: Lat 50.123456 Lon -125.654321\n"
P = B.parse_request
check("mt DAY uses the device GPS", P("mt 17" + GARMIN) == (50.123456, -125.654321, "mt", 17, []))
check("mt17 without a space", P("Mt17" + GARMIN) == (50.123456, -125.654321, "mt", 17, []))
check("mt LAT LON DAY", P("mt 51.23 -123.90 17") == (51.23, -123.9, "mt", 17, []))
check("longitude minus optional", P("mt 51.23 123.90 17") == (51.23, -123.9, "mt", 17, []))
check("elevations", P("mt 51.23 -123.90 17 2500 3500") == (51.23, -123.9, "mt", 17, [2500, 3500]))
check("elevations with GPS, m suffix and slash", P("mt 17 2500m/3500m" + GARMIN) == (50.123456, -125.654321, "mt", 17, [2500, 3500]))
check("inReach keyboard '51. 23'", P("mt 51. 23 -123. 9 17") == (51.23, -123.9, "mt", 17, []))
check("date is mandatory", P("mt" + GARMIN) == "mt-help" and P("mt 51.23 -123.90") == "mt-help")
check("two dates rejected", P("mt 17 18" + GARMIN) == "mt-help")
check("elevation out of range rejected", P("mt 17 8000" + GARMIN) == "mt-help" and P("mt 17 400" + GARMIN) == "mt-help")
check("whole-number coordinates rejected, not misread", P("mt 51 -124 17") == "mt-help")
check("no GPS in the email", P("mt 17") == "gps-missing")
check("wx still parses", P("wx 49.36 -125.72") == (49.36, -125.72, "wx"))
check("wv still parses with GPS", P("wv" + GARMIN) == (50.123456, -125.654321, "wv"))
check("wx wins when it comes first", P("wx 49.36 -125.72 near Mt Baker")[2] == "wx")
check("help texts fit one message", len(B.HELP) <= 160 and len(B.MT_HELP) <= 160)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
raise SystemExit(1 if FAILS else 0)
