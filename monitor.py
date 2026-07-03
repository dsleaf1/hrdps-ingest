#!/usr/bin/env python3
"""Log HRDPS release timing + map-data staleness to R2 hrdps/release_log.csv.
Records, each run: newest run available on ECCC Datamart (continental + 1km) and the
run currently served in R2 (what the map shows), with ages and remaining future hours.
Run ~every 2h to characterise how often the 48h forecast's future coverage drops <24h."""
import datetime as dt, json, subprocess, os, sys
import ingest   # reuse Datamart run-finding

R2="https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev"
now=dt.datetime.now(dt.timezone.utc)
def age_h(x): return round((now-x).total_seconds()/3600,2) if x else None
def ecc_latest(model):
    try:
        ingest.M=ingest.MODELS[model]; ingest.RES=ingest.M["res"]
        base,run,sample=ingest.find_latest_run()
        return ingest.run_datetime(sample)
    except Exception as e:
        print(f"ecc {model} probe failed: {e}",file=sys.stderr); return None
def r2_run(prefix):
    try:
        txt=subprocess.run(["curl","-s",f"{R2}/{prefix}/manifest.json?cb={now.timestamp()}"],capture_output=True,text=True).stdout
        return dt.datetime.strptime(json.loads(txt)["run"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception as e:
        print(f"r2 {prefix} failed: {e}",file=sys.stderr); return None
ec,ek=ecc_latest("continental"),ecc_latest("hrdps_west")
rc,rk=r2_run("hrdps"),r2_run("hrdps1km")
def fut(x): a=age_h(x); return round(48-a,2) if a is not None else None
def iso(x,h=False): return x.strftime("%Y-%m-%dT%HZ") if x else ""
cols=[now.strftime("%Y-%m-%dT%H:%M:%SZ"),
      iso(ec),age_h(ec), iso(ek),age_h(ek),
      iso(rc),age_h(rc),fut(rc), iso(rk),age_h(rk),fut(rk)]
row=",".join("" if c is None else str(c) for c in cols)
HDR="observed_utc,ecc_cont_run,ecc_cont_age_h,ecc_1km_run,ecc_1km_age_h,r2_cont_run,r2_cont_age_h,r2_cont_future_h,r2_1km_run,r2_1km_age_h,r2_1km_future_h"
print(HDR); print(row)
if "--no-upload" not in sys.argv:
    import boto3
    B=os.environ.get("R2_BUCKET","hrdps")
    s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    try: existing=s3.get_object(Bucket=B,Key="hrdps/release_log.csv")["Body"].read().decode()
    except Exception: existing=""
    body=(existing if existing else HDR+"\n").rstrip("\n")+"\n"+row+"\n"
    s3.put_object(Bucket=B,Key="hrdps/release_log.csv",Body=body.encode(),ContentType="text/csv",CacheControl="max-age=300")
    print(f"logged ({body.count(chr(10))-1} rows total)",file=sys.stderr)
