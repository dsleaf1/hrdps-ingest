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
 # Broughtons — user-requested points (2026-07-03; names provisional, in-SSCOFS-domain verified)
 ("broughtons-discovery","Retreat Passage",50.651028,-126.760238),
 ("broughtons-discovery","Sutlej Channel",50.807757,-126.520566),
 ("broughtons-discovery","Mackenzie Sound",50.939046,-126.480698),
 # Northern Georgia Strait
 ("northern-georgia-strait","Georgia Strait central",49.600,-124.600),
 ("northern-georgia-strait","Georgia Strait N",49.880,-124.880),
 ("northern-georgia-strait","Malaspina Strait",49.700,-124.320),
 ("northern-georgia-strait","Sabine Channel",49.500,-124.200),
 ("northern-georgia-strait","off Nanaimo",49.220,-123.920),
 ("northern-georgia-strait","Georgia Strait S-central",49.320,-124.180),
]

# Field tiles: full-region SSCOFS surface-current windows byte-packed like the wind field.
# bbox [W,S,E,N] matches the wind map REGIONS so the current map aligns. Validated core first.
FIELD_REGIONS={
 "puget-sound":            [-123.3, 47.0, -122.2, 48.4],
 "san-juan-gulf-islands":  [-123.95, 48.3, -122.5, 49.3],
 "northern-georgia-strait":[-125.4, 49.0, -123.4, 50.2],
}
# Whole-extent zoomed-out layer for the NANOOS-style big map (SSCOFS_Big_Map_Plan.md step 2):
# Puget Sound -> QCS/model top, decimated by OVERVIEW_STRIDE (server-side DAP stride) to ~2 km.
OVERVIEW_BBOX=[-129.0, 46.9, -122.0, 52.13]
OVERVIEW_STRIDE=4

def pack_field(day,cyc,ymd,bbox,hrs,tmo=180,stride=1):
    """Fetch the SSCOFS surface u,v window for a region bbox each hour and byte-pack it
    like the wind field: per hour = rows*cols speed bytes (kt*10, 255=nodata) then rows*cols
    dir bytes (toward-bearing * 255/360). Row 0 = NORTH, col 0 = WEST. stride>1 decimates
    server-side (DAP [lo:stride:hi]; THREDDS prints dense row indices so parse2d is unchanged);
    cell spacing becomes DX*stride. Returns (cols, rows, center_bbox[W,S,E,N], bytearray)."""
    W,S,E,N=bbox
    iy0=round((S-LAT0)/DX); iy1=round((N-LAT0)/DX)      # iy grows north
    ix0=round((W-LON0)/DX); ix1=round((E-LON0)/DX)      # ix grows east
    iy0=iy1-((iy1-iy0)//stride)*stride                   # snap so the N/E edges are kept
    ix1=ix0+((ix1-ix0)//stride)*stride
    cols,rows=(ix1-ix0)//stride+1,(iy1-iy0)//stride+1
    cbbox=[round(LON0+ix0*DX,4),round(LAT0+iy0*DX,4),round(LON0+ix1*DX,4),round(LAT0+iy1*DX,4)]
    buf=bytearray()
    for kind,num,off in hrs:
        url=f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}.regulargrid.{kind}{num:03d}.nc.ascii?"+enc(
            f"u_eastward[0][0][{iy0}:{stride}:{iy1}][{ix0}:{stride}:{ix1}],v_northward[0][0][{iy0}:{stride}:{iy1}][{ix0}:{stride}:{ix1}]")
        txt=curl(url,tmo)
        U=parse2d(txt,"u_eastward"); V=parse2d(txt,"v_northward")
        spd=bytearray(rows*cols); drr=bytearray(rows*cols); k=0; nwater=0
        for orow in range(rows):            # north-first output
            r=rows-1-orow                   # grid row (0=south)
            for c in range(cols):
                u=U.get((r,c)); v=V.get((r,c))
                if u is None or v is None or abs(u)>100 or abs(v)>100:
                    spd[k]=255; drr[k]=0
                else:
                    b=round(math.hypot(u,v)*KT*10)      # kt*10
                    spd[k]=min(b,254)
                    drr[k]=round((math.degrees(math.atan2(u,v))%360)*255/360)%256
                    nwater+=1
                k+=1
        buf+=spd+drr
        print(f"    {kind}{num:03d}: {nwater}/{rows*cols} water",file=sys.stderr)
    return cols,rows,cbbox,buf

def field_main(a):
    import gzip
    day,cyc,ymd=latest_cycle()
    run=dt.datetime.strptime(ymd+cyc,"%Y%m%dt%Hz").replace(tzinfo=dt.timezone.utc)
    print(f"FIELD cycle {day} {cyc} run={run.isoformat()}",file=sys.stderr)
    keys=[k for k in a.regions.split(",") if k] or list(FIELD_REGIONS)+["overview"]
    hrs=[("f",h,h) for h in range(a.hours+1)]                       # f000..fNN
    hours_ms=[int((run+dt.timedelta(hours=off)).timestamp()*1000) for _,_,off in hrs]
    manifest={"model":"SSCOFS surface current","run":run.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "generated":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "res_deg":DX,"nodata":255,"speed_unit":"kt_x10","dir_convention":"toward",
              "hours":hours_ms,"regions":{}}
    blobs={}
    for reg in keys:
        stride=OVERVIEW_STRIDE if reg=="overview" else 1
        bbox=OVERVIEW_BBOX if reg=="overview" else FIELD_REGIONS[reg]
        cols,rows,cbbox,buf=pack_field(day,cyc,ymd,bbox,hrs,stride=stride)
        manifest["regions"][reg]={"name":reg,"bbox":cbbox,"cols":cols,"rows":rows,
                                  "res_deg":DX*stride,"file":f"{reg}.bin"}
        blobs[reg]=bytes(buf)
        print(f"  {reg}: {cols}x{rows} x{len(hrs)}h -> {len(buf)} bytes",file=sys.stderr)
    if a.no_upload:
        os.makedirs("field_out",exist_ok=True)
        json.dump(manifest,open("field_out/manifest.json","w"),indent=1)
        for reg,b in blobs.items(): open(f"field_out/{reg}.bin","wb").write(b)
        print("wrote ./field_out",file=sys.stderr); return
    import boto3
    BUCKET=os.environ.get("R2_BUCKET","hrdps")
    s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    s3.put_object(Bucket=BUCKET,Key="sscofs/field/manifest.json",
        Body=gzip.compress(json.dumps(manifest,separators=(",",":")).encode()),
        ContentType="application/json",ContentEncoding="gzip",CacheControl="max-age=600")
    for reg,b in blobs.items():
        s3.put_object(Bucket=BUCKET,Key=f"sscofs/field/{reg}.bin",Body=gzip.compress(b),
            ContentType="application/octet-stream",ContentEncoding="gzip",CacheControl="max-age=900")
    print(f"uploaded sscofs/field/manifest.json + {len(blobs)} region tiles",file=sys.stderr)

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

def archive_broughtons(stationlist, run, s3, bucket):
    """Append the nowcast/analysis portion (valid<=run) of each Broughtons&Discovery
    station to a monthly CSV in R2, deduped by (time,station). Consecutive 6-hourly
    cycles tile into a continuous hourly record of predicted currents across the region."""
    run_ms=run.timestamp()*1000
    key=f"sscofs/archive/broughtons_{run.strftime('%Y%m')}.csv"
    try: existing=s3.get_object(Bucket=bucket,Key=key)["Body"].read().decode()
    except Exception: existing=""
    seen=set()
    for line in existing.splitlines()[1:]:
        p=line.split(",")
        if len(p)>=2: seen.add((p[0],p[1]))
    new=[]
    for s in stationlist:
        if s["region"]!="broughtons-discovery": continue
        for tms,u,v in s["ev"]:
            if tms>run_ms+1: continue                     # nowcast + analysis only (valid <= run)
            iso=dt.datetime.fromtimestamp(tms/1000,dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if (iso,s["name"]) in seen: continue
            seen.add((iso,s["name"]))
            spd=math.hypot(u,v); drr=math.degrees(math.atan2(u,v))%360
            new.append(f"{iso},{s['name']},{spd:.2f},{drr:.0f},{u},{v}")
    if not new:
        print("archive: no new rows",file=sys.stderr); return
    body=(existing if existing else "time_utc,station,speed_kt,dir_deg,u_kt,v_kt\n").rstrip("\n")
    body=body+"\n"+"\n".join(sorted(new))+"\n"
    s3.put_object(Bucket=bucket,Key=key,Body=body.encode(),ContentType="text/csv",CacheControl="max-age=300")
    print(f"archived {len(new)} rows -> {key} ({body.count(chr(10))-1} total)",file=sys.stderr)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--hours",type=int,default=48)
    ap.add_argument("--regions",default=""); ap.add_argument("--no-upload",action="store_true")
    ap.add_argument("--field",action="store_true",help="build current-FIELD tiles (not point stations)")
    a=ap.parse_args()
    if a.field: return field_main(a)
    day,cyc,ymd=latest_cycle()
    run=dt.datetime.strptime(ymd+cyc,"%Y%m%dt%Hz").replace(tzinfo=dt.timezone.utc)
    print(f"cycle {day} {cyc} run={run.isoformat()}",file=sys.stderr)
    keys=[k for k in a.regions.split(",") if k] or sorted(set(s[0] for s in STATIONS))
    st=[s for s in STATIONS if s[0] in keys]
    # group by region, snap
    out={"model":"SSCOFS","run":run.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "generated":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "hours":[],"stations":[]}
    # prepend nowcast n001..n005 (= run-5h .. run-1h) so the series covers wind
    # timelines that start before this cycle; then forecast f000..fNN
    hrs=[("n",n,n-6) for n in range(1,6)] + [("f",h,h) for h in range(a.hours+1)]
    out["hours"]=[int((run+dt.timedelta(hours=off)).timestamp()*1000) for _,_,off in hrs]
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
        for hi,(kind,num,off) in enumerate(hrs):
            url=f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}.regulargrid.{kind}{num:03d}.nc.ascii?"+enc(
                f"u_eastward[0][0][{iy0}:{iy1}][{ix0}:{ix1}],v_northward[0][0][{iy0}:{iy1}][{ix0}:{ix1}]")
            txt=curl(url)
            U=parse2d(txt,"u_eastward"); V=parse2d(txt,"v_northward")
            tms=out["hours"][hi]
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
        import boto3, gzip
        BUCKET=os.environ.get("R2_BUCKET","hrdps")
        s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        s3.put_object(Bucket=BUCKET,Key="sscofs/stations.json",Body=gzip.compress(js.encode()),
            ContentType="application/json",ContentEncoding="gzip",CacheControl="max-age=600")
        print("uploaded sscofs/stations.json",file=sys.stderr)
        try: archive_broughtons(stationlist, run, s3, BUCKET)      # long-term Broughtons record
        except Exception as e: print("archive failed:",e,file=sys.stderr)

if __name__=="__main__": main()
