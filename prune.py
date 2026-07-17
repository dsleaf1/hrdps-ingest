#!/usr/bin/env python3
"""Prune season-archive days older than a cutoff. MANUAL invocation only
(Model_Replay_Plan.md step 1(5)).

Deletes archive/wind/<region>/<date>.{bin,json}, archive/current/<date>.json and
archive/obs/<date>.json for dates strictly before --before. Never touches
archive/wind/full/ (flagged full-run captures), any date pinned in
archive/flags.json, or derived catalogs (index/windows/divergence/events/
flags/subregions). Dry run by default; pass --yes to delete (then the index
is rebuilt).

Needs R2_ENDPOINT / R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

  python prune.py --before 20261101          # show what would go
  python prune.py --before 20261101 --yes    # actually delete
"""
import os, re, sys, argparse
import archive_util as au

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="YYYYMMDD; delete strictly older days")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    a = ap.parse_args()
    if not re.fullmatch(r"\d{8}", a.before):
        raise SystemExit("--before must be YYYYMMDD")
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    log = lambda *x: print(*x, file=sys.stderr)
    doomed, _ = au.prune_days(s3, os.environ["R2_BUCKET"], a.before, delete=a.yes, log=log)
    for k in doomed:
        log(("DELETED " if a.yes else "would delete ") + k)
    if not a.yes and doomed:
        log("dry run — pass --yes to delete")

if __name__ == "__main__":
    main()
