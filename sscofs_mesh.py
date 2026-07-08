#!/usr/bin/env python3
"""Extract SSCOFS NATIVE FVCOM mesh surface currents -> R2 (for the high-res mesh map).

Unlike sscofs.py (which uses the 0.005 deg regulargrid product, ~500 m), this reads the
native `fields` product: an unstructured triangular mesh (~240k nodes / 433k elements)
refined to ~40-90 m in narrows — enough to resolve features like the Tacoma Narrows eddy.

Per region (elements whose centroid falls in the bbox):
  - static geometry  -> R2 sscofs/mesh/{region}.json  {nodes:[[lon,lat]...], tris:[[a,b,c]...]}
    (uploaded once; the mesh never changes. tris order == velocity element order)
  - per-cycle vel    -> R2 sscofs/meshvel/{region}.bin  per hour: n_elem speed bytes (kt*10,
    255=dry) then n_elem dir bytes (toward-bearing*255/360); offset = h*2*n_elem
  - sscofs/meshvel/manifest.json {run,hours[ms],speed_unit,dir_convention,regions{key:{n_elem,file,mesh}}}

Velocity fetched via .dods binary (5x smaller than ascii). Needs numpy.
Local test:  python sscofs_mesh.py --hours 2 --regions puget-sound --no-upload
"""
import subprocess, re, sys, os, json, math, argparse, gzip
import datetime as dt
import numpy as np

OPENDAP="https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS"
S3="https://noaa-nos-ofs-pds.s3.amazonaws.com"
KT=1.94384

REGIONS={
 "puget-sound":            [-123.3, 47.0, -122.2, 48.4],
 "san-juan-gulf-islands":  [-123.95, 48.3, -122.5, 49.3],
 "northern-georgia-strait":[-125.4, 49.0, -123.4, 50.2],
}

def log(*a): print(*a, file=sys.stderr, flush=True)
def enc(q): return q.replace("[","%5B").replace("]","%5D")

def curl(url, tmo=120, binary=True):
    for _ in range(3):
        r=subprocess.run(["curl","-sg","--max-time",str(tmo),url],capture_output=True)
        if r.returncode==0 and r.stdout and b"Error {" not in r.stdout[:200]: return r.stdout
    return r.stdout

def dods(fileurl, var, dtype=">f4"):
    """Fetch one variable via .dods binary -> flat numpy array."""
    b=curl(f"{fileurl}.dods?{enc(var)}")
    i=b.find(b"\nData:\n")
    if i<0: raise RuntimeError("no Data marker for "+var)
    i+=7
    n=int(np.frombuffer(b[i:i+4],dtype=">i4")[0])       # length marker (repeated twice)
    return np.frombuffer(b[i+8:i+8+n*4], dtype=dtype).astype("float64" if dtype==">f4" else "int64")

def latest_cycle():
    for back in range(2):
        d=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=back)).strftime("%Y/%m/%d")
        xml=curl(f"{S3}/?list-type=2&prefix=sscofs/netcdf/{d}/&max-keys=1000",30).decode("utf-8","replace")
        cyc=sorted(set(re.findall(r"sscofs\.(t\d\dz)\.(\d{8})\.fields\.f048\.nc",xml)))
        if cyc: t,ymd=cyc[-1]; return d,t,ymd
    raise SystemExit("no SSCOFS cycle with fields.f048")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--hours",type=int,default=48)
    ap.add_argument("--regions",default="")
    ap.add_argument("--no-upload",action="store_true")
    a=ap.parse_args()
    keys=[k for k in a.regions.split(",") if k] or list(REGIONS)

    day,cyc,ymd=latest_cycle()
    run=dt.datetime.strptime(ymd+cyc,"%Y%m%dt%Hz").replace(tzinfo=dt.timezone.utc)
    base=f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}"
    f0=f"{base}.fields.f000.nc"
    log(f"cycle {day} {cyc} run={run.isoformat()}")

    # ---- static geometry (node lon/lat + connectivity) ----
    lon=dods(f0,"lon[0:239733]")-360.0
    lat=dods(f0,"lat[0:239733]")
    nv=dods(f0,"nv[0:2][0:433409]",">i4").reshape(3,-1)-1        # (3,nele) 0-based
    nele=nv.shape[1]
    clon=lon[nv].mean(0); clat=lat[nv].mean(0)                   # element centroids
    log(f"mesh: {len(lon)} nodes, {nele} elements")

    reg_elems={}
    for key in keys:
        W,S,E,N=REGIONS[key]
        elems=np.where((clon>=W)&(clon<=E)&(clat>=S)&(clat<=N))[0]
        reg_elems[key]=elems
        log(f"  {key}: {len(elems)} elements")

    s3=bucket=None
    if not a.no_upload:
        import boto3
        bucket=os.environ.get("R2_BUCKET","hrdps")
        s3=boto3.client("s3",endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])

    # ---- per-region static mesh geometry (upload once; skip if present) ----
    def put(key,data,ctype):
        s3.put_object(Bucket=bucket,Key=key,Body=gzip.compress(data),ContentType=ctype,
                      ContentEncoding="gzip",CacheControl="public, max-age=86400")
    def exists(key):
        try: s3.head_object(Bucket=bucket,Key=key); return True
        except Exception: return False
    for key in keys:
        elems=reg_elems[key]
        used=np.unique(nv[:,elems])                              # global node ids used
        remap=np.full(len(lon),-1,dtype=np.int64); remap[used]=np.arange(len(used))
        nodes=np.column_stack([lon[used],lat[used]]).round(5)
        tris=remap[nv[:,elems]].T                                # (n_elem,3) local node idx
        mesh={"nodes":nodes.tolist(),"tris":tris.tolist()}
        js=json.dumps(mesh,separators=(",",":")).encode()
        mkey=f"sscofs/mesh/{key}.json"
        if a.no_upload:
            os.makedirs("mesh_out",exist_ok=True); open(f"mesh_out/{key}.json","wb").write(js)
            log(f"  {key} mesh: {len(nodes)} nodes {len(tris)} tris {len(js)} bytes (local)")
        elif not exists(mkey):
            put(mkey,js,"application/json"); log(f"  uploaded {mkey} ({len(js)} B)")
        else:
            log(f"  {mkey} exists, skip")

    # ---- per-hour surface velocity ----
    hrs=list(range(a.hours+1))
    hours_ms=[int((run+dt.timedelta(hours=h)).timestamp()*1000) for h in hrs]
    velbuf={k:bytearray() for k in keys}
    for h in hrs:
        fu=f"{base}.fields.f{h:03d}.nc"
        u=dods(fu,"u[0][0][0:433409]"); v=dods(fu,"v[0][0][0:433409]")
        dry=(np.abs(u)>100)|(np.abs(v)>100)
        spd=np.hypot(u,v)*KT
        sb=np.clip(np.round(spd*10),0,254).astype(np.uint8); sb[dry]=255
        db=(np.round((np.degrees(np.arctan2(u,v))%360)*255/360).astype(np.int32)%256).astype(np.uint8)
        for k in keys:
            e=reg_elems[k]; velbuf[k]+=sb[e].tobytes()+db[e].tobytes()
        log(f"  f{h:03d}: max {spd.max():.1f} kt")

    manifest={"model":"SSCOFS surface current (native FVCOM mesh)","run":run.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "generated":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "speed_unit":"kt_x10","dir_convention":"toward","hours":hours_ms,
              "regions":{k:{"n_elem":int(len(reg_elems[k])),"file":f"{k}.bin","mesh":f"mesh/{k}.json"} for k in keys}}
    if a.no_upload:
        json.dump(manifest,open("mesh_out/manifest.json","w"),indent=1)
        for k in keys: open(f"mesh_out/{k}.bin","wb").write(bytes(velbuf[k]))
        log("wrote ./mesh_out")
    else:
        put("sscofs/meshvel/manifest.json",json.dumps(manifest,separators=(",",":")).encode(),"application/json")
        for k in keys: put(f"sscofs/meshvel/{k}.bin",bytes(velbuf[k]),"application/octet-stream")
        log(f"uploaded meshvel manifest + {len(keys)} region velocity bins")

if __name__=="__main__": main()
