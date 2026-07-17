#!/usr/bin/env python3
"""
Season archive for the model-replay exercise (cape_st_james/Model_Replay_Plan.md step 1).

Alongside each pipeline's latest-overwrite flow, the day's data is retained under
archive/ in the same R2 bucket:

  archive/wind/<region>/<YYYYMMDD>.bin   stitched best-lead wind day (24 UTC hour slots)
  archive/wind/<region>/<YYYYMMDD>.json  sidecar: dims + per-slot source run
  archive/current/<YYYYMMDD>.json        SSCOFS station series per run (valid <= run+6h)
  archive/obs/<YYYYMMDD>.json            hourly obs snapshots
  archive/index.json                     season manifest, rebuilt from a bucket LIST

Stitching rule everywhere: freshest run wins on overlap. For wind that happens at
write time (per-slot source-run compare); for currents each run's overlapping series
is stored and the reader takes the newest run covering an hour.

index.json is derived data, rebuilt from a LIST by every archiver: concurrent writers
(wind 4x/day, currents 4x/day, obs hourly) can leave it stale for at most one cycle,
never wrong about what it does list. Sizes in the index are stored (gzipped) bytes.

Archiving is unconditional on upload runs (dev-reference invariant 6) and failures
must fail the job so GitHub alerts — the raw archive is the one irreplaceable layer.

All objects are gzip-encoded like the rest of the bucket (ContentEncoding: gzip).
Stdlib only (callers hand in a boto3 client).
"""
import re, gzip, json, datetime as dt

V = 1
UTC = dt.timezone.utc

FORMATS = {
    "wind_bin": ("24 UTC hour slots (00..23Z), fixed size. Per slot: rows*cols uint8 wind "
                 "speed km/h (255 = nodata / slot never written) then rows*cols uint8 "
                 "direction-from (deg * 255/360). Row 0 = north, col 0 = west — identical "
                 "packing to the live per-region .bin, one hour per slot. Dims and per-slot "
                 "source run are in the <YYYYMMDD>.json sidecar."),
    "wind_sidecar": "{v, date, region, bbox[W,S,E,N], cols, rows, res_deg, src: [24 x run ISO | null]}",
    "current": ("{v, date, runs: [{run, stations: [{name, region, lat, lon, "
                "ev: [[epoch_ms, u_kt, v_kt], ...]}]}]} — each SSCOFS cycle's series trimmed "
                "to valid <= run+6h (nowcast + best-lead forecast), points filed under their "
                "valid UTC date; where runs overlap an hour, take the newest run."),
    "obs": "{v, date, snaps: [{t, stations: [obs.py station records]}, ...]} hourly snapshots",
}

def _noop(*a, **k): pass

# ---------- gzip'd R2 object helpers ----------
def get_gz(s3, bucket, key):
    """Decompressed bytes for key, or None if absent."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return None
    try:
        return gzip.decompress(body)
    except OSError:
        return body                     # stored uncompressed

def put_gz(s3, bucket, key, data, content_type, max_age=300):
    s3.put_object(Bucket=bucket, Key=key, Body=gzip.compress(data),
                  ContentType=content_type, ContentEncoding="gzip",
                  CacheControl=f"public, max-age={max_age}")

def get_json(s3, bucket, key):
    b = get_gz(s3, bucket, key)
    return None if b is None else json.loads(b)

def put_json(s3, bucket, key, obj, max_age=300):
    put_gz(s3, bucket, key, json.dumps(obj, separators=(",", ":")).encode(),
           "application/json", max_age)

# ---------- wind ----------
def _nodata_slot(cols, rows):
    return b"\xff" * (cols * rows) + b"\x00" * (cols * rows)

def archive_wind_slots(s3, bucket, region, meta, blobs, run_iso, log=_noop):
    """Overlay one run's hour blobs onto the region's day file(s).

    blobs = [(valid_dt_utc, slot_bytes), ...]; a run near midnight spills into the
    next day's file. A slot is only overwritten when this run is at least as fresh
    as the slot's recorded source run (ISO strings compare chronologically), so
    out-of-order re-runs cannot clobber fresher data. Returns {date: slots_written}.
    """
    cols, rows = meta["cols"], meta["rows"]
    n = cols * rows * 2
    bydate = {}
    for vt, blob in blobs:
        if len(blob) != n:
            raise ValueError(f"blob size {len(blob)} != {n} for {region}")
        bydate.setdefault(vt.strftime("%Y%m%d"), []).append((vt.hour, blob))
    written = {}
    for date, slots in sorted(bydate.items()):
        bkey = f"archive/wind/{region}/{date}.bin"
        skey = f"archive/wind/{region}/{date}.json"
        day = get_gz(s3, bucket, bkey)
        side = get_json(s3, bucket, skey)
        if (day is None or side is None or len(day) != 24 * n
                or side.get("cols") != cols or side.get("rows") != rows):
            if day is not None:
                log(f"  archive wind {region}/{date}: dims changed, reinitializing day file")
            day = bytearray(_nodata_slot(cols, rows) * 24)
            side = {"v": V, "date": date, "region": region, "bbox": meta["bbox"],
                    "cols": cols, "rows": rows, "res_deg": meta["res_deg"], "src": [None] * 24}
        else:
            day = bytearray(day)
        wrote = 0
        for hour, blob in slots:
            prev = side["src"][hour]
            if prev is None or prev <= run_iso:        # freshest run wins on overlap
                day[hour * n:(hour + 1) * n] = blob
                side["src"][hour] = run_iso
                wrote += 1
        if wrote:
            put_gz(s3, bucket, bkey, bytes(day), "application/octet-stream")
            put_json(s3, bucket, skey, side)
        written[date] = wrote
        log(f"  archive wind {region}/{date}: {wrote}/{len(slots)} slots from run {run_iso}")
    return written

# ---------- SSCOFS currents ----------
def archive_current_run(s3, bucket, run, stationlist, lead_hours=6, log=_noop):
    """File one SSCOFS cycle's station series (valid <= run+lead_hours, i.e. the
    5 nowcast hours + best-lead forecast) into per-day archive docs; a series
    crossing midnight lands in both days. Re-running a cycle replaces its record."""
    run_iso = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_ms = (run + dt.timedelta(hours=lead_hours)).timestamp() * 1000 + 1
    bydate = {}
    for s in stationlist:
        for p in s["ev"]:
            if p[0] > cutoff_ms:
                continue
            d = dt.datetime.fromtimestamp(p[0] / 1000, UTC).strftime("%Y%m%d")
            ent = bydate.setdefault(d, {}).setdefault(
                s["name"], {"name": s["name"], "region": s["region"],
                            "lat": s["lat"], "lon": s["lon"], "ev": []})
            ent["ev"].append(p)
    written = {}
    for d, stns in sorted(bydate.items()):
        key = f"archive/current/{d}.json"
        doc = get_json(s3, bucket, key) or {"v": V, "date": d, "runs": []}
        doc["runs"] = [r for r in doc["runs"] if r["run"] != run_iso]
        doc["runs"].append({"run": run_iso,
                            "stations": sorted(stns.values(),
                                               key=lambda x: (x["region"], x["name"]))})
        doc["runs"].sort(key=lambda r: r["run"])
        put_json(s3, bucket, key, doc)
        written[d] = len(stns)
        log(f"  archive current {d}: run {run_iso}, {len(stns)} stations")
    return written

# ---------- obs ----------
def archive_obs_snapshot(s3, bucket, now_iso, stations, log=_noop):
    """Append one hourly obs snapshot to its day doc (same-timestamp re-run replaces)."""
    d = now_iso[:10].replace("-", "")
    key = f"archive/obs/{d}.json"
    doc = get_json(s3, bucket, key) or {"v": V, "date": d, "snaps": []}
    doc["snaps"] = [sn for sn in doc["snaps"] if sn["t"] != now_iso]
    doc["snaps"].append({"t": now_iso, "stations": stations})
    doc["snaps"].sort(key=lambda sn: sn["t"])
    put_json(s3, bucket, key, doc)
    log(f"  archive obs {d}: {len(doc['snaps'])} snapshots")
    return len(doc["snaps"])

# ---------- index ----------
_WIND_RE = re.compile(r"^archive/wind/(?!full/)([^/]+)/(\d{8})\.bin$")
_CUR_RE = re.compile(r"^archive/current/(\d{8})\.json$")
_OBS_RE = re.compile(r"^archive/obs/(\d{8})\.json$")

def list_archive(s3, bucket):
    """{key: stored_size} for every key under archive/."""
    out, token = {}, None
    while True:
        kw = dict(Bucket=bucket, Prefix="archive/", MaxKeys=1000)
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            out[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            break
        token = r["NextContinuationToken"]
    return out

def update_index(s3, bucket, log=_noop):
    """Rebuild archive/index.json (dates available, regions, stored sizes) from a
    bucket LIST so the replay viewer can enumerate without listing the bucket."""
    keys = list_archive(s3, bucket)
    wind, current, obs = {}, {}, {}
    for k, size in keys.items():
        m = _WIND_RE.match(k)
        if m:
            wind.setdefault(m.group(1), {})[m.group(2)] = size
            continue
        m = _CUR_RE.match(k)
        if m:
            current[m.group(1)] = size
            continue
        m = _OBS_RE.match(k)
        if m:
            obs[m.group(1)] = size
    idx = {"v": V,
           "generated": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "note": "sizes are stored (gzip) bytes; wind sidecar = same path .json",
           "formats": FORMATS, "wind": wind, "current": current, "obs": obs}
    put_json(s3, bucket, "archive/index.json", idx)
    log(f"  archive index: {sum(len(d) for d in wind.values())} wind day-files, "
        f"{len(current)} current days, {len(obs)} obs days")
    return idx

# ---------- prune ----------
def pinned_dates(s3, bucket):
    """Dates that must never be pruned: any YYYYMMDD mentioned in archive/flags.json.
    Deliberately liberal — the flags schema lands in step 1e."""
    raw = get_gz(s3, bucket, "archive/flags.json")
    return set() if raw is None else {m.decode() for m in re.findall(rb"\d{8}", raw)}

_DAY_RE = re.compile(r"^archive/(?:wind/(?!full/)[^/]+|current|obs)/(\d{8})\.(?:bin|json)$")

def prune_days(s3, bucket, before, delete=False, log=_noop):
    """Drop per-day archive objects strictly older than `before` (YYYYMMDD). Never
    touches archive/wind/full/ captures, pinned dates, or derived catalogs
    (index/windows/divergence/events/flags/subregions). Dry run unless delete=True.
    Returns (doomed_keys, pinned_dates_skipped)."""
    pinned = pinned_dates(s3, bucket)
    keys = list_archive(s3, bucket)
    doomed, skipped = [], set()
    for k in sorted(keys):
        m = _DAY_RE.match(k)
        if not m or m.group(1) >= before:
            continue
        if m.group(1) in pinned:
            skipped.add(m.group(1))
            continue
        doomed.append(k)
    total = sum(keys[k] for k in doomed)
    log(f"prune before {before}: {len(doomed)} objects ({total/1e6:.1f} MB stored)"
        + (f"; pinned dates kept: {sorted(skipped)}" if skipped else ""))
    if delete and doomed:
        for i in range(0, len(doomed), 1000):
            s3.delete_objects(Bucket=bucket,
                              Delete={"Objects": [{"Key": k} for k in doomed[i:i + 1000]]})
        update_index(s3, bucket, log=log)
    return doomed, skipped
