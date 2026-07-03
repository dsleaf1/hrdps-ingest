#!/usr/bin/env python3
"""Log HRDPS release timing + map-data staleness to R2 hrdps/release_log.csv.
Self-contained (curl only): records newest run available on ECCC Datamart (2.5km +
1km) and the run currently served in R2, with ages and remaining-future hours."""
import datetime as dt, json, subprocess, os, sys, re
R2="https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev"
now=dt.datetime.now(dt.timezone.utc)
def curl(url):
    return subprocess.run(["curl","-s","--max-time","60","-A","hrdps-monitor",url],capture_output=True,text=True).stdout
def links(url): return re.findall(r'href="([^"?][^"]*)"', curl(url))
MODELS={
 "continental":{"base":"https://dd.weather.gc.ca/{date}/WXO-DD/model_hrdps/continental/2.5km","wind":"WIND_AGL-10m"},
 "hrdps_west":{"base":"https://dd.alpha.weather.gc.ca/model_hrdps/west/1km/grib2","wind":"WIND_TGL_10"},
}
def bases(m):
    b=MODELS[m]["base"]
    return [b] if "{date}" not in b else [b.format(date=(now-dt.timedelta(days=d)).strftime("%Y%m%d")) for d in (0,1)]
def ecc_latest(m):
    W=MODELS[m]["wind"]
    for b in bases(m):
        runs=sorted({l.strip("/") for l in links(b+"/") if re.fullmatch(r"\d{2}/",l)},reverse=True)
        for run in runs:
            wind=[x for x in links(f"{b}/{run}/048/") if W in x and x.endswith(".grib2")]
            if wind:
                mm=re.search(r"(\d{8})T(\d{2})Z",wind[0])
                if mm: return dt.datetime.strptime(mm.group(1),"%Y%m%d").replace(hour=int(mm.group(2)),tzinfo=dt.timezone.utc)
    return None
def r2_run(prefix):
    try: return dt.datetime.strptime(json.loads(curl(f"{R2}/{prefix}/manifest.json?cb={now.timestamp()}"))["run"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception: return None
def age(x): return round((now-x).total_seconds()/3600,2) if x else None
def fut(x): a=age(x); return round(48-a,2) if a is not None else None
def iso(x): return x.strftime("%Y-%m-%dT%HZ") if x else ""
ec,ek,rc,rk=ecc_latest("continental"),ecc_latest("hrdps_west"),r2_run("hrdps"),r2_run("hrdps1km")
cols=[now.strftime("%Y-%m-%dT%H:%M:%SZ"),iso(ec),age(ec),iso(ek),age(ek),iso(rc),age(rc),fut(rc),iso(rk),age(rk),fut(rk)]
row=",".join("" if c is None else str(c) for c in cols)
HDR="observed_utc,ecc_cont_run,ecc_cont_age_h,ecc_1km_run,ecc_1km_age_h,r2_cont_run,r2_cont_age_h,r2_cont_future_h,r2_1km_run,r2_1km_age_h,r2_1km_future_h"
print(HDR); print(row)
if "--no-upload" not in sys.argv:
    import boto3
    B=os.environ.get("R2_BUCKET","hrdps")
    s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    try: existing=s3.get_object(Bucket=B,Key="hrdps/release_log.csv")["Body"].read().decode()
    except Exception: existing=""
    body=(existing if existing else HDR+"\n").rstrip("\n")+"\n"+row+"\n"
    s3.put_object(Bucket=B,Key="hrdps/release_log.csv",Body=body.encode(),ContentType="text/csv",CacheControl="max-age=300")
    print(f"logged ({body.count(chr(10))-1} rows)",file=sys.stderr)
