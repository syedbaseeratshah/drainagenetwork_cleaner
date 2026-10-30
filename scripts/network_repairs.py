"""
Level repairs for a SWMM-style Conduit/Junctions network, applied after the
topology has been cleaned (network_checks.py handles topology, this file
handles elevations).

Two repairs, both generalised from the Chirag Delhi pipeline (its stages
17b and 18/19). Neither invents data: every change is written to a review
table and the value before the change is kept in an *_ORIG column.

  fix_invert_spikes   a single junction whose invert is wrong makes the
                      conduit next to it run uphill. The invert is replaced
                      by the value interpolated between its neighbours, but
                      only when the pattern is unambiguous (one node
                      standing out of the line) and the fix makes nothing
                      else run uphill.

  raise_rims          a closed pipe taller than rim minus invert does not
                      fit inside its manhole. The rim is raised to the
                      ground (when a DEM is given and the ground is at or
                      above the pipe crown) or to the pipe crown. With a DEM,
                      rims below the ground are raised to it as well, except
                      where the survey and the DEM disagree by more than
                      max_gap (a levelling error, not a wrong manhole).

Field names follow the SWMM export: INVERTELEV, RIMELEV, DEPTH on junctions;
INLETNODE, OUTLETNODE, LENGTH, SLOPE, XSECTION, GEOM1, INOFFSET, OUTOFFSET on
conduits. Missing optional fields (offsets, SLOPE) are skipped.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CLOSED_SHAPES = {"RECT_CLOSED", "CIRCULAR"}
TOLERANCE = 0.001  # m, rounding tolerance for level comparisons


def _offsets(conduit):
    zero = pd.Series(0.0, index=conduit.index)
    inoff = conduit["INOFFSET"].fillna(0) if "INOFFSET" in conduit else zero
    outoff = conduit["OUTOFFSET"].fillna(0) if "OUTOFFSET" in conduit else zero
    return inoff, outoff


def _is_junction(junctions) -> pd.Series:
    if "NODETYPE" in junctions:
        return junctions["NODETYPE"].astype(str).str.upper() == "JUNCTION"
    names = junctions["NAME"].astype(str).str.upper()
    return ~(names.str.startswith("OST_") | names.str.startswith("SST_"))


def pipe_heights(conduit) -> pd.Series:
    """Per node: tallest closed pipe attached, measured from the node invert (offset + GEOM1)."""
    closed = conduit[conduit["XSECTION"].isin(CLOSED_SHAPES)].copy()
    inoff, outoff = _offsets(closed)
    ends = pd.concat([
        pd.Series((closed["GEOM1"] + inoff).values, index=closed["INLETNODE"].values),
        pd.Series((closed["GEOM1"] + outoff).values, index=closed["OUTLETNODE"].values),
    ])
    return ends.groupby(level=0).max()


# ---------------------------------------------------------------------------
# Invert spikes and pits
# ---------------------------------------------------------------------------

def fix_invert_spikes(conduit, junctions, rise_min: float = 0.5, out_min: float = 0.5):
    """
    Returns (conduit, junctions, review) where review has one row per conduit
    rising more than rise_min, with the decision and the reason.

    A conduit rising more than rise_min is corrected at one end only:
      spike: its outlet stands more than out_min above the inlet and above every
             downstream neighbour, while the inlet is at or above those neighbours.
      pit:   its inlet stands more than out_min below the outlet and below every
             upstream neighbour, while those neighbours are at or above the outlet.
    The new invert is interpolated by conduit length between the neighbours
    (median when there are several). It is rejected if any attached conduit would
    still run uphill, if it reaches the surveyed rim, or if a closed pipe that
    fitted under the rim before would no longer fit.
    """
    cd = conduit.copy()
    jn = junctions.copy().set_index("NAME", drop=False)
    inv = jn["INVERTELEV"].astype(float).copy()
    inoff, outoff = _offsets(cd)
    pipe_h = pipe_heights(cd)

    up: dict = {}    # node -> [(conduit, upstream node, length)]
    down: dict = {}  # node -> [(conduit, downstream node, length)]
    for r in cd.itertuples():
        down.setdefault(r.INLETNODE, []).append((r.NAME, r.OUTLETNODE, r.LENGTH))
        up.setdefault(r.OUTLETNODE, []).append((r.NAME, r.INLETNODE, r.LENGTH))

    def interpolate(ups, downs):
        vals = [inv[u] - (inv[u] - inv[d]) * lu / (lu + ld) for _, u, lu in ups for _, d, ld in downs]
        return float(np.median(vals))

    def uphill_after(node, z):
        lv = inv.copy()
        lv[node] = z
        return ([c for c, d, _ in down.get(node, []) if lv[d] > lv[node] + 1e-6]
                + [c for c, u, _ in up.get(node, []) if lv[node] > lv[u] + 1e-6])

    rise = (cd["OUTLETNODE"].map(inv) + outoff) - (cd["INLETNODE"].map(inv) + inoff)
    rows, fixes = [], {}
    for r in cd.assign(rise=rise)[rise > rise_min].sort_values("rise", ascending=False).itertuples():
        i, o = r.INLETNODE, r.OUTLETNODE
        row = {"conduit": r.NAME, "inlet": i, "outlet": o, "length_m": round(float(r.LENGTH), 1),
               "rise_m": round(float(r.rise), 2), "inlet_inv": round(float(inv[i]), 2), "outlet_inv": round(float(inv[o]), 2)}
        dn_o = [t for t in down.get(o, []) if t[1] in inv.index]
        up_i = [t for t in up.get(i, []) if t[1] in inv.index]
        spike = (bool(dn_o) and inv[o] - inv[i] > out_min
                 and all(inv[o] - inv[d] > out_min and inv[i] >= inv[d] for _, d, _ in dn_o))
        pit = (bool(up_i) and inv[o] - inv[i] > out_min
               and all(inv[u] - inv[i] > out_min and inv[u] >= inv[o] for _, u, _ in up_i))
        node, z = None, np.nan
        if spike and pit:
            kind = "both ends fit, not decided"
        elif spike:
            node, kind, ups, downs = o, "spike at outlet", [(r.NAME, i, r.LENGTH)], dn_o
        elif pit:
            node, kind, ups, downs = i, "pit at inlet", up_i, [(r.NAME, o, r.LENGTH)]
        elif not dn_o and not up_i:
            kind = "dead end at both sides"
        else:
            kind = "no single-node pattern (uphill run over several nodes or neighbour levels mixed)"

        if node is not None:
            z = interpolate(ups, downs)
            rim = jn.at[node, "RIMELEV"]
            h = pipe_h.get(node, 0)
            bad = uphill_after(node, z)
            if node in fixes:
                kind += ", node already corrected by another conduit"
                z = fixes[node]
            elif bad:
                kind += f", rejected: {', '.join(bad)} would run uphill"
                node = None
            elif z >= rim:
                kind += f", rejected: new invert {z:.2f} at or above the surveyed rim {rim:.2f}"
                node = None
            elif z + h > rim + TOLERANCE >= inv[node] + h:
                kind += ", rejected: the closed pipe would rise above the surveyed rim"
                node = None
            else:
                fixes[node] = z
        row.update({"pattern": kind, "corrected_node": node or "",
                    "new_inv": round(z, 2) if node else np.nan,
                    "shift_m": round(z - inv[node], 2) if node else np.nan})
        rows.append(row)

    jn["INV_ORIG"] = jn["INVERTELEV"]
    jn["INV_FIX"] = jn["NAME"].isin(fixes).astype(int)
    for n, z in fixes.items():
        jn.at[n, "INVERTELEV"] = round(z, 3)
        if "RIMELEV" in jn and "DEPTH" in jn:
            jn.at[n, "DEPTH"] = round(jn.at[n, "RIMELEV"] - z, 3)
    jn["INV_SHIFT"] = (jn["INVERTELEV"] - jn["INV_ORIG"]).round(3)

    if "SLOPE" in cd:
        cd["SLOPE_ORIG"] = cd["SLOPE"]
        touch = cd["INLETNODE"].isin(fixes) | cd["OUTLETNODE"].isin(fixes)
        z_in = cd["INLETNODE"].map(jn["INVERTELEV"]) + inoff
        z_out = cd["OUTLETNODE"].map(jn["INVERTELEV"]) + outoff
        cd.loc[touch, "SLOPE"] = ((z_in - z_out) / cd["LENGTH"])[touch].round(6)

    review = pd.DataFrame(rows)
    return cd, jn.reset_index(drop=True), review


# ---------------------------------------------------------------------------
# Rims
# ---------------------------------------------------------------------------

def ground_level(dem_path, points, radius: float = 15.0) -> np.ndarray:
    """Lowest valid DEM value within radius of each point (NaN where every cell is void)."""
    import rasterio
    from rasterio.windows import Window

    out = np.full(len(points), np.nan)
    with rasterio.open(dem_path) as src:
        if src.crs is not None and points.crs is not None and src.crs != points.crs:
            points = points.to_crs(src.crs)
        r = int(np.ceil(radius / src.res[0]))
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1] * src.res[0]
        outside = np.hypot(xx, yy) > radius
        for k, pt in enumerate(points):
            row, col = src.index(pt.x, pt.y)
            a = src.read(1, window=Window(col - r, row - r, 2 * r + 1, 2 * r + 1),
                         boundless=True, fill_value=np.nan).astype(float)
            if src.nodata is not None:
                a[a == src.nodata] = np.nan
            a[outside] = np.nan
            if np.isfinite(a).any():
                out[k] = np.nanmin(a)
    return out


def raise_rims(conduit, junctions, dem_path=None, ground_radius: float = 15.0, max_gap: float | None = 3.0):
    """
    Returns (junctions, review). Rims are only ever raised, never lowered.

    A junction is "flagged" when its tallest closed pipe is taller than rim minus invert.
      flagged, DEM given, ground at or above the crown, no level conflict -> rim = ground  (RIM_FIX)
      any other flagged junction                                         -> rim = crown   (RIM_CROWN)
      unflagged, DEM given, ground above the rim, no level conflict      -> rim = ground  (RIM_UP_DEM)
    Level conflict (RIM_CONFL): ground more than max_gap above the surveyed rim. Those rims are kept,
    because raising them would build manholes several metres tall that hide overflow.
    Only nodes typed JUNCTION change. DEPTH is recomputed for every changed rim.
    """
    jn = junctions.copy()
    isj = _is_junction(jn)
    jn["PIPE_H"] = jn["NAME"].map(pipe_heights(conduit)).fillna(0.0)
    jn["CROWN"] = jn["INVERTELEV"] + jn["PIPE_H"]
    jn["RIM_ORIG"] = jn["RIMELEV"]

    flagged = isj & (jn["PIPE_H"] > jn["RIMELEV"] - jn["INVERTELEV"] + TOLERANCE)
    if dem_path:
        jn["DEM_GRND"] = np.round(ground_level(dem_path, jn.geometry, ground_radius), 3)
        ground = jn["DEM_GRND"]
        conflict = (isj & ground.notna() & (ground - jn["RIMELEV"] > max_gap)) if max_gap is not None \
            else pd.Series(False, index=jn.index)
        fix = flagged & ~conflict & ground.notna() & (ground >= jn["CROWN"])
        up = isj & ~flagged & ~conflict & ground.notna() & (ground - jn["RIMELEV"] > TOLERANCE)
    else:
        conflict = fix = up = pd.Series(False, index=jn.index)
    crown = flagged & ~fix

    jn["RIM_CONFL"] = conflict.astype(int)
    jn["RIM_FIX"] = fix.astype(int)
    jn["RIM_CROWN"] = crown.astype(int)
    jn["RIM_UP_DEM"] = up.astype(int)
    if dem_path:
        jn.loc[fix | up, "RIMELEV"] = jn.loc[fix | up, "DEM_GRND"]
    jn.loc[crown, "RIMELEV"] = jn.loc[crown, "CROWN"]
    changed = fix | crown | up
    jn.loc[changed, "DEPTH"] = (jn.loc[changed, "RIMELEV"] - jn.loc[changed, "INVERTELEV"]).round(3)
    jn["RIM_RAISE"] = (jn["RIMELEV"] - jn["RIM_ORIG"]).round(3)

    rule = np.select([fix, crown, up], ["rim = ground", "rim = pipe crown", "rim = ground (was below it)"], "")
    review = jn.loc[changed, ["NAME", "INVERTELEV", "RIM_ORIG", "RIMELEV", "RIM_RAISE", "DEPTH", "PIPE_H"]].copy()
    review["rule"] = rule[changed.values]
    return jn, review.sort_values("RIM_RAISE", ascending=False)


# ---------------------------------------------------------------------------
# Read-only detectors used by the highlight tools
# ---------------------------------------------------------------------------

def find_uphill_conduits(conduit, junctions, rise_min: float = 0.5) -> set[str]:
    """Names of conduits whose outlet sits more than rise_min above the inlet."""
    inv = junctions.set_index("NAME")["INVERTELEV"]
    inoff, outoff = _offsets(conduit)
    rise = (conduit["OUTLETNODE"].map(inv) + outoff) - (conduit["INLETNODE"].map(inv) + inoff)
    return set(conduit.loc[rise > rise_min, "NAME"])


def find_pipe_taller_than_node(conduit, junctions) -> set[str]:
    """Names of junctions where a closed pipe is taller than rim minus invert."""
    h = junctions["NAME"].map(pipe_heights(conduit)).fillna(0.0)
    bad = _is_junction(junctions) & (h > junctions["RIMELEV"] - junctions["INVERTELEV"] + TOLERANCE)
    return set(junctions.loc[bad, "NAME"])
