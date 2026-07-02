#!/usr/bin/env python3
"""Extract SSCOFS surface currents at virtual station points -> compact JSON (-> R2).
Uses OPeNDAP .ascii (curl); no netCDF libs. See SSCOFS_PLAN.md."""
import subprocess, re, math, json, sys, os, datetime as dt, argparse
OPENDAP="https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS"
S3="https://noaa-nos-ofs-pds.s3.amazonaws.com"
LAT0,LON0,DX=44.37,-129.53,0.005
KT=1.94384  # m/s -> kt
def idx(lat,lon): return round((lat-LAT0)/DX), round((lon-LON0)/DX)
def enc(q): return q.replace("[","%5B").replace("]","%5D")
def curl(url,tmo=90):
    for a in range(3):
        r=subprocess.run(["curl","-sg","--max-time",str(tmo),url],capture_output=True,text=True)
        if r.returncode==0 and r.stdout and "Error {" not in r.stdout[:200] and "HTTP Status" not in r.stdout[:200]:
            return r.stdout
    return r.stdout
def parse2d(txt,var):
    """dict (r,c)->float from a var hyperslab block; handles any bracket depth.
    Row lines look like '[..][..][r], v0, v1, ...'. Block ends at next var header."""
    m=re.search(re.escape(var)+r"\[\d[^\n]*\n(.*?)(?=\n[A-Za-z_]+\[\d|\Z)", txt, re.S)
    if not m: return {}
    out={}
    for row in re.finditer(r"\[(\d+)\],\s*([-\d.eE, ]+)", m.group(1)):
        r=int(row.group(1)); vals=[float(x) for x in row.group(2).split(",") if x.strip()!=""]
        for c,v in enumerate(vals): out[(r,c)]=v
    return out

STATIONS=[
 # Puget Sound — open-water crossings between the official point stations
 ("puget-sound","Admiralty Inlet mid",48.130,-122.700),
 ("puget-sound","Marrowstone approach",48.030,-122.620),
 ("puget-sound","Hood Canal entrance",47.940,-122.575),
 ("puget-sound","Puget main N",47.800,-122.440),
 ("puget-sound","off Point Jefferson",47.750,-122.470),
 ("puget-sound","Elliott Bay approach",47.620,-122.440),
 ("puget-sound","East Passage N",47.500,-122.440),
 ("puget-sound","East Passage mid",47.400,-122.440),
 ("puget-sound","Colvos Passage",47.420,-122.535),
 ("puget-sound","Dalco / Narrows N",47.310,-122.520),
 ("puget-sound","Carr Inlet mouth",47.240,-122.665),
 ("puget-sound","Nisqually Reach mid",47.140,-122.780),
 # San Juan & Gulf Islands — open-water crossings
 ("san-juan-gulf-islands","Rosario Strait mid",48.460,-122.750),
 ("san-juan-gulf-islands","San Juan Channel mid",48.550,-123.000),
 ("san-juan-gulf-islands","Haro Strait mid",48.530,-123.190),
 ("san-juan-gulf-islands","Boundary Pass mid",48.720,-123.080),
 ("san-juan-gulf-islands","President Channel",48.660,-122.980),
 ("san-juan-gulf-islands","Georgia Strait entrance",48.900,-123.150),
 ("san-juan-gulf-islands","Juan de Fuca N",48.280,-123.100),
 # Broughtons & Discovery
 ("broughtons-discovery","Johnstone Strait W",50.450,-126.200),
 ("broughtons-discovery","Johnstone Strait mid",50.370,-125.850),
 ("broughtons-discovery","Discovery Passage",50.130,-125.350),
 ("broughtons-discovery","Queen Charlotte Strait",50.720,-126.850),
 ("broughtons-discovery","Blackfish Sound",50.550,-126.600),
 ("broughtons-discovery","Broughton Strait",50.580,-126.920),
 ("broughtons-discovery","Weynton Passage",50.580,-126.750),
 # Northern Georgia Strait
 ("northern-georgia-strait","Georgia Strait central",49.600,-124.600),
 ("northern-georgia-strait","Georgia Strait N",49.880,-124.880),
 ("northern-georgia-strait","Malaspina Strait",49.700,-124.320),
 ("northern-georgia-strait","Sabine Channel",49.500,-124.200),
 ("northern-georgia-strait","off Nanaimo",49.220,-123.920),
 ("northern-georgia-strait","Georgia Strait S-central",49.320,-124.180),
]

def latest_cycle():
    for back in range(2):
        d=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=back)).strftime("%Y/%m/%d")
        xml=curl(f"{S3}/?list-type=2&prefix=sscofs/netcdf/{d}/&max-keys=1000",30)
        cyc=sorted(set(re.findall(r"sscofs\.(t\d\dz)\.(\d{8})\.regulargrid\.f048\.nc",xml)))
        if cyc:
            t,ymd=cyc[-1]
            return d, t, ymd
    raise SystemExit("no SSCOFS cycle with f048 found")

def snap_region(day,cyc,ymd,stns,pad=9):
    """fetch mask window, snap each station to nearest water cell."""
    ys=[idx(la,lo)[0] for _,_,la,lo in stns]; xs=[idx(la,lo)[1] for _,_,la,lo in stns]
    iy0,iy1=min(ys)-pad,max(ys)+pad; ix0,ix1=min(xs)-pad,max(xs)+pad
    url=f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}.regulargrid.f000.nc.ascii?"+enc(f"mask[{iy0}:{iy1}][{ix0}:{ix1}]")
    txt=curl(url); ny,nx=iy1-iy0+1,ix1-ix0+1
    mask=parse2d(txt,"mask")
    snapped=[]
    for reg,nm,la,lo in stns:
        iy,ix=idx(la,lo); r0,c0=iy-iy0,ix-ix0; best=None;bd=1e9
        for dr in range(-pad,pad+1):
            for dc in range(-pad,pad+1):
                if mask.get((r0+dr,c0+dc))==1.0:
                    d=dr*dr+dc*dc
                    if d<bd: bd=d;best=(iy+dr,ix+dc)
        snapped.append((reg,nm,la,lo,best))
    return (iy0,iy1,ix0,ix1),snapped

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--hours",type=int,default=48)
    ap.add_argument("--regions",default=""); ap.add_argument("--no-upload",action="store_true")
    a=ap.parse_args()
    day,cyc,ymd=latest_cycle()
    run=dt.datetime.strptime(ymd+cyc,"%Y%m%dt%Hz").replace(tzinfo=dt.timezone.utc)
    print(f"cycle {day} {cyc} run={run.isoformat()}",file=sys.stderr)
    keys=[k for k in a.regions.split(",") if k] or sorted(set(s[0] for s in STATIONS))
    st=[s for s in STATIONS if s[0] in keys]
    # group by region, snap
    out={"model":"SSCOFS","run":run.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "generated":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "hours":[],"stations":[]}
    hrs=list(range(a.hours+1))
    out["hours"]=[int((run+dt.timedelta(hours=h)).timestamp()*1000) for h in hrs]
    regmeta={}
    for reg in keys:
        rs=[s for s in st if s[0]==reg]
        win,snapped=snap_region(day,cyc,ymd,rs)
        regmeta[reg]=(win,snapped)
        nwater=sum(1 for x in snapped if x[4])
        print(f"  {reg}: {len(rs)} stns, {nwater} snapped to water, window {win}",file=sys.stderr)
    # per hour fetch u,v window per region
    stationlist=[]
    for reg in keys:
        (iy0,iy1,ix0,ix1),snapped=regmeta[reg]; ny,nx=iy1-iy0+1,ix1-ix0+1
        series={nm:[] for _,nm,_,_,_ in snapped}
        for h in hrs:
            url=f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}.regulargrid.f{h:03d}.nc.ascii?"+enc(
                f"u_eastward[0][0][{iy0}:{iy1}][{ix0}:{ix1}],v_northward[0][0][{iy0}:{iy1}][{ix0}:{ix1}]")
            txt=curl(url)
            U=parse2d(txt,"u_eastward"); V=parse2d(txt,"v_northward")
            tms=out["hours"][h]
            for reg2,nm,la,lo,cell in snapped:
                if not cell: continue
                u=U.get((cell[0]-iy0,cell[1]-ix0)); v=V.get((cell[0]-iy0,cell[1]-ix0))
                if u is None or u<-9000 or v is None: continue
                series[nm].append([tms, round(u*KT,3), round(v*KT,3)])
        for reg2,nm,la,lo,cell in snapped:
            if cell and series[nm]:
                gla=LAT0+cell[0]*DX; glo=LON0+cell[1]*DX
                stationlist.append({"name":nm,"region":reg,"lat":round(gla,4),"lon":round(glo,4),"ev":series[nm]})
    out["stations"]=stationlist
    for s in stationlist:
        sp=[math.hypot(u,v) for _,u,v in s["ev"]]
        print(f"    {s['region'][:12]:12s} {s['name']:26s} max {max(sp):.1f} kt (n={len(sp)})",file=sys.stderr)
    js=json.dumps(out,separators=(",",":"))
    open("sscofs_stations.json","w").write(js)
    print(f"wrote sscofs_stations.json  {len(stationlist)} stations  {len(js)} bytes",file=sys.stderr)
    if not a.no_upload:
        import boto3
        s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        import gzip
        s3.put_object(Bucket=os.environ.get("R2_BUCKET","hrdps"),Key="sscofs/stations.json",Body=gzip.compress(js.encode()),
            ContentType="application/json",ContentEncoding="gzip",CacheControl="max-age=600")
        print("uploaded sscofs/stations.json",file=sys.stderr)

if __name__=="__main__": main()
