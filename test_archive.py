#!/usr/bin/env python3
"""Offline regression tests for archive_util (Model Replay step 1) against an
in-memory S3 stub. Run: python test_archive.py — prints PASS/FAIL per case."""
import io, gzip, json, datetime as dt
import archive_util as au

UTC = dt.timezone.utc
FAILS = []

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)

class FakeS3:
    def __init__(self):
        self.store = {}
    def put_object(self, Bucket, Key, Body, **kw):
        self.store[Key] = Body
    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.store[Key])}
    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        ks = sorted(k for k in self.store if k.startswith(Prefix))
        return {"Contents": [{"Key": k, "Size": len(self.store[k])} for k in ks],
                "IsTruncated": False}
    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.store.pop(o["Key"], None)
        return {}

B = "bucket"
META = {"bbox": [-127.0, 50.0, -126.955, 50.0225], "cols": 3, "rows": 2, "res_deg": 0.0225}
N = META["cols"] * META["rows"] * 2      # bytes per hour slot

def run_blobs(run_dt, tag, hours=7):
    """One fake run: hours 0..6, every byte = a tag identifying (run, hour)."""
    return [(run_dt + dt.timedelta(hours=h), bytes([tag * 10 + h] * N)) for h in range(hours)]

def day_bin(s3, region, date):
    return gzip.decompress(s3.store[f"archive/wind/{region}/{date}.bin"])

def day_side(s3, region, date):
    return json.loads(gzip.decompress(s3.store[f"archive/wind/{region}/{date}.json"]))

def slot(buf, hour):
    return buf[hour * N:(hour + 1) * N]

# ---------- wind stitching ----------
s3 = FakeS3()
d0 = dt.datetime(2026, 7, 16, tzinfo=UTC)
runs = [(d0 + dt.timedelta(hours=hh), tag) for hh, tag in [(0, 1), (6, 2), (12, 3), (18, 4)]]
for run_dt, tag in runs:
    au.archive_wind_slots(s3, B, "test-region", META,
                          run_blobs(run_dt, tag), run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

bin16 = day_bin(s3, "test-region", "20260716")
side16 = day_side(s3, "test-region", "20260716")
check("wind day file is 24 slots", len(bin16) == 24 * N)
own = {h: (1 if h < 6 else 2 if h < 12 else 3 if h < 18 else 4) for h in range(24)}
ok = all(slot(bin16, h) == bytes([own[h] * 10 + (h % 6)] * N) for h in range(24))
check("freshest run wins on every overlap slot (00/06/12/18Z tiling)", ok)
check("sidecar src records the owning run",
      side16["src"][0].startswith("2026-07-16T00") and side16["src"][23].startswith("2026-07-16T18"))

# 18Z hour 6 spills into the 17th, slot 0
bin17 = day_bin(s3, "test-region", "20260717")
check("run near midnight spills into next day's file", slot(bin17, 0) == bytes([46] * N))
check("unwritten slots are nodata (speed 255)",
      slot(bin17, 5)[:N // 2] == b"\xff" * (N // 2))

# next day's 00Z run overwrites the spilled 6-h-lead slot
d1 = dt.datetime(2026, 7, 17, tzinfo=UTC)
au.archive_wind_slots(s3, B, "test-region", META, run_blobs(d1, 5), d1.strftime("%Y-%m-%dT%H:%M:%SZ"))
check("next 00Z analysis overwrites previous run's 6-h forecast",
      slot(day_bin(s3, "test-region", "20260717"), 0) == bytes([50] * N))

# a stale (older) run re-applied must NOT clobber fresher slots
au.archive_wind_slots(s3, B, "test-region", META, run_blobs(runs[0][0], 9),
                      runs[0][0].strftime("%Y-%m-%dT%H:%M:%SZ"))
check("equal-run re-apply rewrites its own slots only",
      slot(day_bin(s3, "test-region", "20260716"), 3) == bytes([93] * N)
      and slot(day_bin(s3, "test-region", "20260716"), 6) == bytes([20] * N))
stale = [(d0 + dt.timedelta(hours=8), bytes([77] * N))]
au.archive_wind_slots(s3, B, "test-region", META, stale, "2026-07-15T18:00:00Z")
check("stale run cannot clobber a fresher slot",
      slot(day_bin(s3, "test-region", "20260716"), 8) == bytes([22] * N))

# ---------- current archiving ----------
run = dt.datetime(2026, 7, 16, 21, tzinfo=UTC)      # t21z: +6h crosses midnight
ms = lambda t: int(t.timestamp() * 1000)
ev = [[ms(run + dt.timedelta(hours=h)), 0.5, -0.5] for h in range(-5, 49)]
stn = [{"name": "Weynton Passage", "region": "broughtons-discovery",
        "lat": 50.58, "lon": -126.75, "ev": ev}]
au.archive_current_run(s3, B, run, stn)
cur16 = json.loads(gzip.decompress(s3.store["archive/current/20260716.json"]))
cur17 = json.loads(gzip.decompress(s3.store["archive/current/20260717.json"]))
check("current: series trimmed to run+6h and split across valid dates",
      len(cur16["runs"][0]["stations"][0]["ev"]) == 8      # 16Z..23Z
      and len(cur17["runs"][0]["stations"][0]["ev"]) == 4)  # 00Z..03Z
au.archive_current_run(s3, B, run, stn)
cur16b = json.loads(gzip.decompress(s3.store["archive/current/20260716.json"]))
check("current: re-running a cycle replaces, not duplicates", len(cur16b["runs"]) == 1)

# ---------- obs archiving ----------
obs = [{"id": "46131", "wspd_kt": 12.0}]
au.archive_obs_snapshot(s3, B, "2026-07-16T05:25:00Z", obs)
au.archive_obs_snapshot(s3, B, "2026-07-16T06:25:00Z", obs)
au.archive_obs_snapshot(s3, B, "2026-07-16T06:25:00Z", obs)   # same-t re-run
obs16 = json.loads(gzip.decompress(s3.store["archive/obs/20260716.json"]))
check("obs: snapshots append, same-timestamp re-run dedupes", len(obs16["snaps"]) == 2)

# ---------- index ----------
idx = au.update_index(s3, B)
check("index: wind dates per region",
      set(idx["wind"]["test-region"]) == {"20260716", "20260717"})
check("index: current + obs days listed",
      set(idx["current"]) == {"20260716", "20260717"} and set(idx["obs"]) == {"20260716"})
check("index: sizes are stored bytes",
      idx["wind"]["test-region"]["20260716"] == len(s3.store["archive/wind/test-region/20260716.bin"]))

# ---------- prune ----------
s3.store["archive/wind/full/20260716/2026-07-16T12:00:00Z/test-region.bin"] = b"x"
s3.store["archive/flags.json"] = json.dumps({"pins": ["20260717"]}).encode()
doomed, skipped = au.prune_days(s3, B, "20260718", delete=False)
check("prune dry-run deletes nothing", "archive/obs/20260716.json" in s3.store)
doomed, skipped = au.prune_days(s3, B, "20260718", delete=True)
check("prune: pinned date survives", "archive/wind/test-region/20260717.bin" in s3.store
      and "20260717" in skipped)
check("prune: old day objects removed",
      "archive/wind/test-region/20260716.bin" not in s3.store
      and "archive/wind/test-region/20260716.json" not in s3.store
      and "archive/current/20260716.json" not in s3.store
      and "archive/obs/20260716.json" not in s3.store)
check("prune: flagged full-run capture untouched",
      "archive/wind/full/20260716/2026-07-16T12:00:00Z/test-region.bin" in s3.store)
idx = json.loads(gzip.decompress(s3.store["archive/index.json"]))
check("prune rebuilds the index", set(idx["wind"].get("test-region", {})) == {"20260717"})

print()
if FAILS:
    raise SystemExit(f"{len(FAILS)} FAILED: {FAILS}")
print("all archive tests passed")
