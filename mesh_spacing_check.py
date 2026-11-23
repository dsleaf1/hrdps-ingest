#!/usr/bin/env python3
"""mesh_spacing_check.py — how well does the SSCOFS FVCOM mesh resolve the shallow flats and banks?

One-off diagnostic for the planned water-depth term in the steepness model (Unified_Map_Dev_Reference.md
§7). Reads the SAME native `fields` product as sscofs_mesh.py (reuses its dods()/latest_cycle()), so
nothing new is trusted. Reads only: node lon/lat, connectivity nv, bathymetry h(node), water level
zeta(time,node) and, if the file carries it, FVCOM's own wet/dry flag, for the first N forecast hours.
No R2, no secrets, no upload.

Per site it reports:
  mesh    elements, median / p10 / p90 element edge length (m), "elements across" = √(site area)/edge,
          and the share of the site box the mesh covers (low ⇒ drying ground cut out of the mesh as land)
  depth   h percentiles (m, FVCOM convention: positive DOWN from the model datum; negative = above datum,
          i.e. intertidal ground that only floods at higher water). UNIFORM flags a site where at least half
          the elements sit within 0.1 m of the median depth — a depth floor or fill value, not surveyed bathymetry.
  tide    zeta range, the share of elements whose total depth d = h + ζ falls below 3 m / 1 m at some
          hour, the share that DRY at some hour, and the shallowest d reached (FVCOM keeps a thin film of
          water on dry elements, so that minimum is its dry depth, not zero)
  bake    the 0.005° (~0.56 km) steepness-bake cells over the site that hold no element centroid, and,
          among cells holding ≥ 2 elements, how many are "mixed" — an element shallower than 3 m and one
          deeper than 10 m in the same cell, where a cell average would hide a bank

DRY. If the fields file has FVCOM's wet/dry flag (wet_cells per element, else wet_nodes per node), an
element counts as dry at an hour when the flag says so. Otherwise the fallback is d ≤ DRY_FALLBACK_M,
and the table says which rule was used. The first run (2026-11-22) showed a 5 cm threshold never fires.

Usage:  python mesh_spacing_check.py [--hours 24] [--sites boundary-bay,padilla-bay] [--json out.json]
Writes a markdown table to $GITHUB_STEP_SUMMARY when run in Actions, else to stdout.
"""
import argparse, json, math, os, re, sys
import numpy as np
from sscofs_mesh import dods, latest_cycle, curl, OPENDAP, log

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
    # GNIS "Salmon Bank" bar: 48.42315 N, 122.97629 W (NAD83). Box kept south of San Juan Island
    # (Cattle Point light 48.4506 N). The 2026-11-22 box sat west of the bank and read 113 m median.
    "salmon-bank":        [-123.01, 48.40, -122.94, 48.44],
    "baynes-sound-comox": [-124.95, 49.55, -124.80, 49.70],
}
BAKE_DEG = 0.005          # steep_bake.mjs cell size
R_EARTH = 6371000.0
DRY_FALLBACK_M = 0.20     # used only when the file has no wet/dry flag
UNIFORM_M = 0.1           # an element within this of the site median counts as "at the median"
UNIFORM_SHARE = 0.5       # ≥ this share of elements at the median ⇒ UNIFORM (a floor/fill value, not bathymetry)

def pct(a, q): return float(np.percentile(a, q)) if len(a) else float("nan")

def wet_vars(f0):
    """Names of wet/dry variables in the fields file, from its OPeNDAP DDS (empty list if none)."""
    dds = curl(f"{f0}.dds", 60).decode("utf-8", "replace")
    names = sorted(set(re.findall(r"\b(\w*wet\w*)\s*\[", dds)))
    log(f"wet/dry variables in fields file: {names or 'none'}")
    return names

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

    try: wv = wet_vars(f0)
    except Exception as ex: log(f"DDS read failed ({ex}); using depth fallback for dry"); wv = []
    flag = "wet_cells" if "wet_cells" in wv else "wet_nodes" if "wet_nodes" in wv else None

    sel = {}
    for k in keys:
        W, S, E, N = SITES[k]
        e = np.where((clon >= W) & (clon <= E) & (clat >= S) & (clat <= N))[0]
        sel[k] = e
        log(f"  {k}: {len(e)} elements")
    all_e = np.unique(np.concatenate([e for e in sel.values() if len(e)] or [np.array([], int)]))
    need = np.unique(nv[:, all_e].ravel()) if len(all_e) else np.array([], int)
    loc_n = np.full(NNODE, -1); loc_n[need] = np.arange(len(need))
    loc_e = np.full(NELE, -1); loc_e[all_e] = np.arange(len(all_e))

    zmin = np.full(len(need), np.inf); zmax = np.full(len(need), -np.inf)
    dry_ever = np.zeros(len(all_e), bool); hours_read = 0
    for t in range(a.hours + 1):
        fu = f"{base}.fields.f{t:03d}.nc"
        try:
            z = dods(fu, f"zeta[0][0:{NNODE-1}]")
        except Exception as ex:
            log(f"  f{t:03d}: zeta unavailable ({ex}); stopping at {t} h"); break
        zn = z[need]
        good = np.abs(zn) < 50                                   # drop fill values
        zmin[good] = np.minimum(zmin[good], zn[good]); zmax[good] = np.maximum(zmax[good], zn[good])
        if flag:
            try:
                if flag == "wet_cells":
                    w = dods(fu, f"wet_cells[0][0:{NELE-1}]", ">i4")[all_e]
                    dry_ever |= (w == 0)
                else:
                    w = dods(fu, f"wet_nodes[0][0:{NNODE-1}]", ">i4")[need]
                    dry_ever |= (w[loc_n[nv[:, all_e]]] == 0).any(0)
            except Exception as ex:
                log(f"  f{t:03d}: {flag} unreadable ({ex}); switching to depth fallback"); flag = None
        hours_read = t
        log(f"  f{t:03d}: read")
    dry_rule = f"FVCOM {flag}" if flag else f"d ≤ {DRY_FALLBACK_M:.2f} m (no wet/dry flag in file)"

    rows, out = [], {"cycle": f"{day} {cyc}", "hours_read": hours_read, "dry_rule": dry_rule, "sites": {}}
    for k in keys:
        e = sel[k]; W, S, E, N = SITES[k]
        lat0 = math.radians((S + N) / 2)
        box_area = (math.radians(E - W) * math.cos(lat0) * R_EARTH) * (math.radians(N - S) * R_EARTH)
        if len(e) == 0:
            rows.append(f"| {k} | 0 | — | — | 0% | — | — | — | — | — | — | — |"); out["sites"][k] = {"elements": 0}; continue
        tri = nv[:, e]                                           # (3, n)
        x = np.radians(lon[tri]) * math.cos(lat0) * R_EARTH; y = np.radians(lat[tri]) * R_EARTH
        sides = np.concatenate([np.hypot(x[i] - x[(i + 1) % 3], y[i] - y[(i + 1) % 3]) for i in range(3)])
        edge = float(np.median(sides))
        mesh_area = float(np.sum(0.5 * np.abs((x[1] - x[0]) * (y[2] - y[0]) - (x[2] - x[0]) * (y[1] - y[0]))))
        he = h[tri].min(0)                                       # shallowest node of each element
        idx = loc_n[tri]
        dmin = (h[tri] + zmin[idx]).min(0)                       # shallowest total depth reached over the hours
        dry = dry_ever[loc_e[e]] if flag else (dmin <= DRY_FALLBACK_M)
        frac = lambda m: float(np.mean(m))
        share_at_median = frac(np.abs(he - np.median(he)) < UNIFORM_M)
        uniform = share_at_median >= UNIFORM_SHARE
        # 0.005° bake cells over the site
        ci = np.floor((clon[e] - W) / BAKE_DEG).astype(int); cj = np.floor((clat[e] - S) / BAKE_DEG).astype(int)
        nci, ncj = max(1, math.ceil((E - W) / BAKE_DEG)), max(1, math.ceil((N - S) / BAKE_DEG))
        cell = cj * nci + ci
        cells, counts = np.unique(cell, return_counts=True)
        multi = cells[counts >= 2]
        mixed = sum(1 for c in multi if (he[cell == c] < 3).any() and (he[cell == c] > 10).any())
        zr = float(np.nanmax(zmax[idx]) - np.nanmin(zmin[idx])) if np.isfinite(zmin[idx]).any() else float("nan")
        r = dict(elements=int(len(e)), edge_m_median=round(edge, 1),
                 edge_m_p10=round(pct(sides, 10), 1), edge_m_p90=round(pct(sides, 90), 1),
                 elements_across=round(math.sqrt(box_area) / edge, 1),
                 mesh_cover_of_box=round(mesh_area / box_area, 3),
                 h_p10=round(pct(he, 10), 2), h_median=round(pct(he, 50), 2), h_p90=round(pct(he, 90), 2),
                 h_min=round(float(he.min()), 2), h_uniform=bool(uniform), share_h_at_median=round(share_at_median, 3),
                 share_h_below_datum=round(frac(he < 0), 3),
                 zeta_range_m=round(zr, 2),
                 share_ever_below_1m=round(frac(dmin < 1), 3), share_ever_below_3m=round(frac(dmin < 3), 3),
                 share_ever_dry=round(frac(dry), 3), min_total_depth_m=round(float(np.min(dmin)), 3),
                 bake_cells=nci * ncj, bake_cells_without_element=nci * ncj - len(cells),
                 bake_cells_multi_element=int(len(multi)), bake_cells_mixed=int(mixed))
        out["sites"][k] = r
        hcol = f"{r['h_median']:.1f} / {r['h_p10']:.1f} / {r['h_min']:.1f}" + (f" **UNIFORM** ({100*share_at_median:.0f}% at median)" if uniform else "")
        rows.append(f"| {k} | {r['elements']} | {r['edge_m_median']:.0f} ({r['edge_m_p10']:.0f}–{r['edge_m_p90']:.0f}) | "
                    f"{r['elements_across']:.0f} | {100*r['mesh_cover_of_box']:.0f}% | {hcol} | "
                    f"{100*r['share_h_below_datum']:.0f}% | {r['zeta_range_m']:.2f} | "
                    f"{100*r['share_ever_below_3m']:.0f}% / {100*r['share_ever_below_1m']:.0f}% / {100*r['share_ever_dry']:.0f}% | "
                    f"{r['min_total_depth_m']:.2f} | {r['bake_cells_without_element']}/{r['bake_cells']} | "
                    f"{r['bake_cells_mixed']}/{r['bake_cells_multi_element']} |")

    md = [f"## SSCOFS mesh resolution over shallow sites — cycle {day} {cyc}, zeta f000–f{hours_read:03d}", "",
          f"Dry rule: **{dry_rule}**.", "",
          "| site | elements | edge m, median (p10–p90) | elements across | mesh covers box | h m: median / p10 / min | h < 0 (above datum) | ζ range m | ever d<3 m / <1 m / dry | min d m | 0.56 km cells with no element | mixed / multi-element 0.56 km cells |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|", *rows, "",
          "Reading it: **elements across** ≳ 10 ⇒ the mesh resolves the flat. Low **mesh covers box** with many "
          "**cells with no element** ⇒ ground cut out of the mesh as land (or the box is mostly land). "
          "**UNIFORM** ⇒ h is a floor or fill value at that site, not bathymetry. **ever d<3 m** is the share of the "
          "site where the depth term would act at some hour. **min d** is the shallowest total depth reached — on "
          "drying ground it is FVCOM's dry-film depth. **mixed** counts only cells holding ≥ 2 elements, the only "
          "cells where a cell average can hide a bank."]
    text = "\n".join(md) + "\n"
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f: f.write(text)
    print(text)
    with open(a.json, "w") as f: json.dump(out, f, indent=1)
    log(f"wrote {a.json}")

if __name__ == "__main__": main()
