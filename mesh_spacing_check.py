#!/usr/bin/env python3
"""mesh_spacing_check.py — how well does the SSCOFS FVCOM mesh resolve the shallow flats and banks?

One-off diagnostic for the planned water-depth term in the steepness model (Unified_Map_Dev_Reference.md
§7). Reads the SAME native `fields` product as sscofs_mesh.py (reuses its dods()/latest_cycle()), so
nothing new is trusted. Reads only: node lon/lat, connectivity nv, bathymetry h(node), and water level
zeta(time,node) for the first N forecast hours. No R2, no secrets, no upload.

Per site it reports:
  mesh    elements, median / p10 / p90 element edge length (m), and "elements across" = √(site area)/edge
  depth   h percentiles (m, FVCOM convention: positive DOWN from the model datum; negative = above datum,
          i.e. intertidal ground that only floods at higher water)
  tide    zeta range over the hours read, and the share of elements whose total depth d = h + ζ falls
          below 1 m / 3 m / 0 (dry) at some hour — the elements where depth would change the band
  bake    the 0.005° (~0.56 km) steepness-bake cells over the site: how many hold NO element centroid
          (under-resolved by the mesh), and how many are "mixed" — holding both an element shallower than
          3 m and one deeper than 10 m (where a cell average would hide a bank)

Usage:  python mesh_spacing_check.py [--hours 24] [--sites boundary-bay,padilla-bay] [--json out.json]
Writes a markdown table to $GITHUB_STEP_SUMMARY when run in Actions, else to stdout.
"""
import argparse, json, math, os, sys
import numpy as np
from sscofs_mesh import dods, latest_cycle, OPENDAP, log

NNODE, NELE = 239734, 433410

# Approximate boxes [W, S, E, N]. Coarse on purpose: the point is element sizes over the flat, not the
# exact shoreline. Edit freely; keys are the --sites names.
SITES = {
    "boundary-bay":       [-123.05, 48.98, -122.85, 49.10],
    "roberts-bank":       [-123.25, 49.00, -123.05, 49.10],
    "sturgeon-bank":      [-123.30, 49.10, -123.17, 49.25],
    "lummi-bay":          [-122.72, 48.76, -122.64, 48.82],
    "nooksack-delta":     [-122.62, 48.72, -122.50, 48.80],
    "samish-bay":         [-122.55, 48.58, -122.45, 48.66],
    "padilla-bay":        [-122.58, 48.45, -122.47, 48.58],
    "skagit-bay":         [-122.55, 48.25, -122.40, 48.40],
    "salmon-bank":        [-123.08, 48.40, -122.98, 48.46],
    "baynes-sound-comox": [-124.95, 49.55, -124.80, 49.70],
}
BAKE_DEG = 0.005          # steep_bake.mjs cell size
R_EARTH = 6371000.0

def pct(a, q): return float(np.percentile(a, q)) if len(a) else float("nan")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="forecast hours of zeta to read (0..N); 24 covers ~2 tidal cycles")
    ap.add_argument("--sites", default="", help="comma-separated SITES keys (blank = all)")
    ap.add_argument("--json", default="mesh_spacing.json")
    a = ap.parse_args()
    keys = [k for k in a.sites.split(",") if k] or list(SITES)
    bad = [k for k in keys if k not in SITES]
    if bad: raise SystemExit(f"unknown site(s): {bad}; known: {list(SITES)}")

    day, cyc, ymd = latest_cycle()
    base = f"{OPENDAP}/{day}/sscofs.{cyc}.{ymd}"
    f0 = f"{base}.fields.f000.nc"
    log(f"cycle {day} {cyc}")

    lon = dods(f0, f"lon[0:{NNODE-1}]") - 360.0
    lat = dods(f0, f"lat[0:{NNODE-1}]")
    nv = dods(f0, f"nv[0:2][0:{NELE-1}]", ">i4").reshape(3, -1) - 1
    h = dods(f0, f"h[0:{NNODE-1}]")
    log(f"mesh {len(lon)} nodes / {nv.shape[1]} elements; h range {h.min():.1f}..{h.max():.1f} m")
    clon, clat = lon[nv].mean(0), lat[nv].mean(0)

    # sites first, so zeta is only kept for the nodes we need
    sel = {}
    for k in keys:
        W, S, E, N = SITES[k]
        e = np.where((clon >= W) & (clon <= E) & (clat >= S) & (clat <= N))[0]
        sel[k] = e
        log(f"  {k}: {len(e)} elements")
    need = np.unique(np.concatenate([nv[:, e].ravel() for e in sel.values() if len(e)] or [np.array([], int)]))

    zmin = np.full(len(need), np.inf); zmax = np.full(len(need), -np.inf)
    for t in range(a.hours + 1):
        try:
            z = dods(f"{base}.fields.f{t:03d}.nc", f"zeta[0][0:{NNODE-1}]")
        except Exception as ex:
            log(f"  f{t:03d}: zeta unavailable ({ex}); stopping at {t} h"); break
        zn = z[need]
        good = np.abs(zn) < 50                                   # drop fill values
        zmin[good] = np.minimum(zmin[good], zn[good]); zmax[good] = np.maximum(zmax[good], zn[good])
        log(f"  f{t:03d}: zeta read")
    loc = {n: i for i, n in enumerate(need)}

    rows, out = [], {"cycle": f"{day} {cyc}", "hours": a.hours, "sites": {}}
    for k in keys:
        e = sel[k]; W, S, E, N = SITES[k]
        lat0 = math.radians((S + N) / 2)
        area = (math.radians(E - W) * math.cos(lat0) * R_EARTH) * (math.radians(N - S) * R_EARTH)
        if len(e) == 0:
            rows.append(f"| {k} | 0 | — | — | — | — | — | — | — | — |"); out["sites"][k] = {"elements": 0}; continue
        tri = nv[:, e]                                           # (3, n)
        x = np.radians(lon[tri]) * math.cos(lat0) * R_EARTH; y = np.radians(lat[tri]) * R_EARTH
        sides = np.concatenate([np.hypot(x[i] - x[(i + 1) % 3], y[i] - y[(i + 1) % 3]) for i in range(3)])
        edge = np.median(sides)
        he = h[tri].min(0)                                       # shallowest node of each element
        idx = np.vectorize(loc.get)(tri)
        dmin = (h[tri] + zmin[idx]).min(0)                       # shallowest total depth reached over the hours
        frac = lambda m: float(np.mean(m))
        # 0.005° bake cells over the site
        ci = np.floor((clon[e] - W) / BAKE_DEG).astype(int); cj = np.floor((clat[e] - S) / BAKE_DEG).astype(int)
        nci, ncj = max(1, math.ceil((E - W) / BAKE_DEG)), max(1, math.ceil((N - S) / BAKE_DEG))
        cell = cj * nci + ci
        have = np.unique(cell)
        mixed = sum(1 for c in have if (he[cell == c] < 3).any() and (he[cell == c] > 10).any())
        zr = float(np.nanmax(zmax[idx]) - np.nanmin(zmin[idx])) if np.isfinite(zmin[idx]).any() else float("nan")
        r = dict(elements=int(len(e)), edge_m_median=round(float(edge), 1),
                 edge_m_p10=round(pct(sides, 10), 1), edge_m_p90=round(pct(sides, 90), 1),
                 elements_across=round(math.sqrt(area) / edge, 1),
                 h_p10=round(pct(he, 10), 2), h_median=round(pct(he, 50), 2), h_min=round(float(he.min()), 2),
                 share_h_below_datum=round(frac(he < 0), 3),
                 zeta_range_m=round(zr, 2),
                 share_ever_below_1m=round(frac(dmin < 1), 3), share_ever_below_3m=round(frac(dmin < 3), 3),
                 share_ever_dry=round(frac(dmin <= 0.05), 3),
                 bake_cells=nci * ncj, bake_cells_without_element=nci * ncj - len(have), bake_cells_mixed=mixed)
        out["sites"][k] = r
        rows.append(f"| {k} | {r['elements']} | {r['edge_m_median']:.0f} ({r['edge_m_p10']:.0f}–{r['edge_m_p90']:.0f}) | "
                    f"{r['elements_across']:.0f} | {r['h_median']:.1f} / {r['h_p10']:.1f} / {r['h_min']:.1f} | "
                    f"{100*r['share_h_below_datum']:.0f}% | {r['zeta_range_m']:.2f} | "
                    f"{100*r['share_ever_below_3m']:.0f}% / {100*r['share_ever_below_1m']:.0f}% / {100*r['share_ever_dry']:.0f}% | "
                    f"{r['bake_cells_without_element']}/{r['bake_cells']} | {r['bake_cells_mixed']} |")

    md = [f"## SSCOFS mesh resolution over shallow sites — cycle {day} {cyc}, zeta f000–f{a.hours:03d}", "",
          "| site | elements | edge m, median (p10–p90) | elements across | h m: median / p10 / min | h < 0 (above datum) | ζ range m | ever d<3 m / <1 m / dry | 0.56 km cells with no element | mixed 0.56 km cells (<3 m & >10 m) |",
          "|---|---|---|---|---|---|---|---|---|---|", *rows, "",
          "Reading it: **edge** well under the site's width (elements across ≳ 10) ⇒ the mesh resolves the flat. "
          "**ever d<3 m** is the share of the site where the depth term would act at some hour. "
          "**cells with no element** or many **mixed** cells ⇒ the 0.56 km bake grid, not the mesh, is the limit there. "
          "A large **h < 0** share confirms FVCOM carries intertidal ground (wetting/drying) and fixes the datum sign convention."]
    text = "\n".join(md) + "\n"
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f: f.write(text)
    print(text)
    with open(a.json, "w") as f: json.dump(out, f, indent=1)
    log(f"wrote {a.json}")

if __name__ == "__main__": main()
