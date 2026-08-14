#!/usr/bin/env python3
"""steep_upload.py — push steep_bake.mjs / steep_mask.mjs output to R2.

The baker is Node (physics.js is the one source of truth for the band and it is a JS module —
a third copy of the physics in Python is exactly what this project has spent sessions avoiding),
but every other upload in this repo goes through boto3, so the upload half stays Python and
reuses the same put_object shape as sscofs.py.

    python3 steep_upload.py steep_out            # bake output  -> sscofs/steep/
    python3 steep_upload.py steep_out --static   # mask.bin/json -> sscofs/steep/static/

🚨 ⛔ This writes ONLY under sscofs/steep/. It must never touch sscofs/field/overview.bin — the
COAST map (coast-wind-map) reads that same object, so baking anything into it would silently
change a live map's currents.
"""
import gzip, json, os, sys, mimetypes

PREFIX = "sscofs/steep/"

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    static = "--static" in sys.argv
    d = args[0] if args else "steep_out"
    prefix = PREFIX + ("static/" if static else "")

    names = (["mask.json", "mask.bin"] if static
             else ["manifest.json"] + sorted(f for f in os.listdir(d) if f.endswith(".bin")))
    missing = [n for n in names if not os.path.exists(os.path.join(d, n))]
    if missing:
        raise SystemExit(f"missing in {d}: {', '.join(missing)}")

    import boto3
    bucket = os.environ.get("R2_BUCKET", "hrdps")
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

    total = 0
    for n in names:
        body = open(os.path.join(d, n), "rb").read()
        gz = gzip.compress(body, 9)
        ct = "application/json" if n.endswith(".json") else "application/octet-stream"
        # the static mask never changes; the per-run planes follow sscofs/field's own cadence
        cache = "max-age=31536000, immutable" if static else ("max-age=600" if n.endswith(".json") else "max-age=900")
        key = prefix + n
        assert key.startswith(PREFIX), key
        s3.put_object(Bucket=bucket, Key=key, Body=gz, ContentType=ct,
                      ContentEncoding="gzip", CacheControl=cache)
        total += len(gz)
        print(f"  {key:44s} {len(body):>10,} B -> {len(gz):>9,} gz", file=sys.stderr)
    print(f"uploaded {len(names)} objects to {prefix} ({total/1024:.1f} KB on the wire)", file=sys.stderr)

if __name__ == "__main__":
    main()
