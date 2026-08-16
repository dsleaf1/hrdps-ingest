#!/usr/bin/env python3
"""divergence_upload.py — push divergence_scan.mjs output to R2.

    python3 divergence_upload.py scan_out

Uploads:
  scan_out/<YYYYMMDD>.json  ->  archive/divergence/<YYYYMMDD>.json
  scan_out/events.json      ->  archive/events.json
"""
import gzip, json, os, re, sys

PREFIX = "archive/"

def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "scan_out"
    if not os.path.isdir(d):
        raise SystemExit(f"{d} is not a directory")

    day_files = sorted(f for f in os.listdir(d) if re.match(r"\d{8}\.json$", f))
    has_events = os.path.exists(os.path.join(d, "events.json"))
    if not day_files and not has_events:
        print("nothing to upload", file=sys.stderr)
        return

    import boto3
    bucket = os.environ.get("R2_BUCKET", "hrdps")
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

    total = 0
    for f in day_files:
        body = open(os.path.join(d, f), "rb").read()
        gz = gzip.compress(body, 9)
        key = PREFIX + "divergence/" + f
        s3.put_object(Bucket=bucket, Key=key, Body=gz, ContentType="application/json",
                      ContentEncoding="gzip", CacheControl="max-age=86400")
        total += len(gz)
        print(f"  {key:50s} {len(body):>10,} B -> {len(gz):>9,} gz", file=sys.stderr)

    if has_events:
        body = open(os.path.join(d, "events.json"), "rb").read()
        gz = gzip.compress(body, 9)
        key = PREFIX + "events.json"
        s3.put_object(Bucket=bucket, Key=key, Body=gz, ContentType="application/json",
                      ContentEncoding="gzip", CacheControl="max-age=3600")
        total += len(gz)
        print(f"  {key:50s} {len(body):>10,} B -> {len(gz):>9,} gz", file=sys.stderr)

    n = len(day_files) + (1 if has_events else 0)
    print(f"uploaded {n} objects to {PREFIX} ({total/1024:.1f} KB on the wire)", file=sys.stderr)

if __name__ == "__main__":
    main()
