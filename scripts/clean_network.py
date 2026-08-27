#!/usr/bin/env python
"""
Pipeline 2 of 2: actually fix everything identify_flaws.py finds.
Reads the same raw Conduit/Junctions shapefiles, never overwrites them,
and writes a fully cleaned pair plus a before/after map.

Usage:
    python clean_network.py
    python clean_network.py --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp

Cleaning steps, applied in this order (each one only ever acts on what
the previous step left behind, same as the identify pipeline's flaw
priority):

  1. Drop dangling conduits (ISLAND + ORPHANEND) - clusters that never
     reach the main network, and true dead-ends inside conduits whose
     own outlet nothing continues from.
  2. Resolve duplicate-geometry pairs (same two nodes, two records) -
     keep the one with a real cross-section dimension, else the one
     with a recorded length, else the lexicographically first NAME.
     Every decision is logged, nothing is silent.
  3. Drop junctions no longer referenced by any surviving conduit.
  4. Drop duplicate-NAME junctions, but only when every field on the
     duplicate copies is byte-identical - if they're not, it's flagged
     in the log and left alone rather than guessed at (a genuine
     naming collision needs a rename, not a deletion).
  5. Convert every remaining true dead-end Junction into a properly
     named, properly typed Outfall (OST_ prefix, NODETYPE=OUTFALL) -
     in SWMM terms a plain Junction assumes continuity, only an Outfall
     has an actual discharge boundary.
  6. Collapse redundant loops: a bifurcation whose branches reconverge
     downstream AND whose destination junctions have matching
     elevations is the same path digitized twice, not a real tributary
     split - the redundant branch is removed.

Output (outputs/clean/):
    conduit_final.shp / .qml     - cleaned conduits, colored by XSECTION
    junctions_final.shp / .qml   - cleaned junctions, colored by NODETYPE
    before_after_map.html        - interactive map: raw network in grey,
                                    everything removed highlighted in red
    clean_report.txt             - what was removed/renamed and why
"""

from __future__ import annotations

import argparse
import colorsys
from pathlib import Path

import geopandas as gpd

import network_checks as checks
from mapping import build_html_map, write_qml_lines, write_qml_points

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_CONDUIT = REPO_ROOT / "05_chirag_delhi" / "01_raw_drainage_network" / "shapefiles" / "Conduit.shp"
DEFAULT_JUNCTIONS = REPO_ROOT / "05_chirag_delhi" / "01_raw_drainage_network" / "shapefiles" / "Junctions.shp"
DEFAULT_OUTDIR = SCRIPT_DIR.parent / "outputs" / "clean"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean a drainage network: remove dangling/duplicate data, fix dead ends, collapse redundant loops.")
    parser.add_argument("--conduit", type=Path, default=DEFAULT_CONDUIT, help="Path to the Conduit line shapefile.")
    parser.add_argument("--junctions", type=Path, default=DEFAULT_JUNCTIONS, help="Path to the Junctions point shapefile.")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR, help="Where to write the cleaned shapefiles, map, and report.")
    return parser.parse_args()


def remove_unreferenced_and_duplicate_junctions(junctions, conduit, log: list[str]):
    unreferenced = checks.find_unreferenced_junctions(junctions, conduit)
    step1 = junctions[~junctions["NAME"].isin(unreferenced)].copy()
    log.append(f"Junctions removed (no longer referenced by any surviving conduit): {len(unreferenced)}")

    dup_groups = checks.find_duplicate_names(step1, "NAME")
    remove_idx = set()
    field_names = [c for c in step1.columns if c != "geometry"]
    for name, idxs in dup_groups.items():
        rows = step1.loc[idxs]
        signatures = set()
        for _, row in rows.iterrows():
            geom = row.geometry
            key = (round(geom.x, 2), round(geom.y, 2)) + tuple(row[f] for f in field_names)
            signatures.add(key)
        if len(signatures) == 1:
            remove_idx.update(idxs[1:])
            log.append(f"Duplicate junction NAME '{name}': {len(idxs)} identical copies, kept first, removed {len(idxs) - 1}")
        else:
            log.append(f"Duplicate junction NAME '{name}': {len(idxs)} copies are NOT identical - left in place, needs manual review/rename")

    step2 = step1[~step1.index.isin(remove_idx)].copy()
    log.append(f"Duplicate-NAME junctions removed (confirmed identical): {len(remove_idx)}")
    return step2


def convert_terminal_outfalls(conduit, junctions, log: list[str]):
    terminals = checks.find_terminal_dead_ends(conduit)
    log.append(f"True dead ends found (will become Outfalls): {len(terminals)}")

    all_node_names = set()
    for _, row in conduit.iterrows():
        all_node_names.add(checks.norm(row["INLETNODE"]))
        all_node_names.add(checks.norm(row["OUTLETNODE"]))
    all_junction_names = {checks.norm(n) for n in junctions["NAME"]}

    rename_map: dict[str, str] = {}
    for old_name in sorted(terminals):
        new_name = (
            checks.OUTFALL_PREFIX + old_name[len(checks.JUNCTION_PREFIX):]
            if old_name.startswith(checks.JUNCTION_PREFIX)
            else checks.OUTFALL_PREFIX + old_name
        )
        if new_name in all_node_names or new_name in all_junction_names:
            log.append(f"Collision: generated Outfall name '{new_name}' for '{old_name}' already exists elsewhere - left unrenamed, needs manual review")
            continue
        rename_map[old_name] = new_name
        log.append(f"Terminal Junction '{old_name}' -> Outfall '{new_name}'")

    conduit = conduit.copy()
    for col in ("INLETNODE", "OUTLETNODE"):
        conduit[col] = conduit[col].apply(lambda v: rename_map.get(checks.norm(v), v))

    junctions = junctions.copy()
    junctions["NODETYPE"] = "JUNCTION"
    for old_name, new_name in rename_map.items():
        mask = junctions["NAME"].apply(checks.norm) == old_name
        junctions.loc[mask, "NAME"] = new_name
        junctions.loc[mask, "NODETYPE"] = "OUTFALL"

    return conduit, junctions


def collapse_redundant_loops(conduit, junctions, log: list[str]):
    redundant_conduits, redundant_junctions = checks.find_redundant_loops(conduit, junctions)
    log.append(f"Redundant loops collapsed: {len(set(redundant_conduits.values()))} loop(s), {len(redundant_conduits)} conduit(s) removed")

    remaining_conduit = conduit[~conduit["NAME"].isin(redundant_conduits.keys())].copy()

    still_referenced = set()
    for _, row in remaining_conduit.iterrows():
        still_referenced.add(checks.norm(row["INLETNODE"]))
        still_referenced.add(checks.norm(row["OUTLETNODE"]))

    drop_junction_names = set()
    for name in redundant_junctions:
        if name in still_referenced:
            log.append(f"Junction '{name}' was on a redundant loop but is still referenced after removal - kept, needs manual review")
        else:
            drop_junction_names.add(name)

    remaining_junctions = junctions[~junctions["NAME"].apply(checks.norm).isin(drop_junction_names)].copy()
    log.append(f"Junctions removed as part of loop collapse: {len(drop_junction_names)}")
    return remaining_conduit, remaining_junctions


def generate_colors(n: int) -> list[str]:
    """Evenly spaced hues around the color wheel - works for any number of categories."""
    colors = []
    for i in range(max(n, 1)):
        r, g, b = colorsys.hsv_to_rgb(i / max(n, 1), 0.55, 0.85)
        colors.append(f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}")
    return colors


def main() -> None:
    args = parse_args()

    if not args.conduit.exists():
        raise FileNotFoundError(f"Conduit shapefile not found: {args.conduit}")
    if not args.junctions.exists():
        raise FileNotFoundError(f"Junctions shapefile not found: {args.junctions}")

    print(f"Reading {args.conduit}")
    raw_conduit = gpd.read_file(args.conduit)
    print(f"Reading {args.junctions}")
    raw_junctions = gpd.read_file(args.junctions)
    print(f"{len(raw_conduit)} conduits, {len(raw_junctions)} junctions (never modified - working from copies from here on)")

    log: list[str] = []

    print("Step 1-2: removing dangling and duplicate-geometry conduits...")
    conduit, sim_log = checks.simulate_clean_conduits(raw_conduit)
    log.extend(sim_log)

    print("Step 3-4: removing unreferenced and duplicate junctions...")
    junctions = remove_unreferenced_and_duplicate_junctions(raw_junctions, conduit, log)

    print("Step 5: converting true dead ends to Outfalls...")
    conduit, junctions = convert_terminal_outfalls(conduit, junctions, log)

    print("Step 6: collapsing redundant loops...")
    conduit, junctions = collapse_redundant_loops(conduit, junctions, log)

    args.outdir.mkdir(parents=True, exist_ok=True)
    conduit_out = args.outdir / "conduit_final.shp"
    junctions_out = args.outdir / "junctions_final.shp"
    conduit.to_file(conduit_out)
    junctions.to_file(junctions_out)

    xsection_values = sorted(v for v in conduit["XSECTION"].dropna().unique() if v)
    conduit_colors = dict(zip(xsection_values, generate_colors(len(xsection_values))))
    write_qml_lines(conduit_out.with_suffix(".qml"), "XSECTION", conduit_colors)
    write_qml_points(junctions_out.with_suffix(".qml"), "NODETYPE", {"OUTFALL": "#d7263d", "JUNCTION": "#7d8f8a"})

    print(f"Cleaned network written to: {args.outdir}")
    print(f"  Conduits:  {len(raw_conduit)} -> {len(conduit)}")
    print(f"  Junctions: {len(raw_junctions)} -> {len(junctions)}")

    report_path = args.outdir / "clean_report.txt"
    report_lines = [
        "Drainage Network - Clean Report",
        "=" * 60,
        f"Conduits:  {len(raw_conduit)} raw -> {len(conduit)} final ({len(raw_conduit) - len(conduit)} removed)",
        f"Junctions: {len(raw_junctions)} raw -> {len(junctions)} final ({len(raw_junctions) - len(junctions)} removed)",
        "",
        "Steps applied, in order:",
    ] + [f"  - {line}" for line in log]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"Report written to: {report_path}")

    print("Building before/after map...")
    raw_wgs84 = raw_conduit.to_crs(4326)
    final_names = set(conduit["NAME"])
    raw_wgs84 = raw_wgs84.copy()
    raw_wgs84["FLAW"] = raw_wgs84["NAME"].apply(lambda n: "OK" if n in final_names else "REMOVED")
    final_junctions_wgs84 = junctions.to_crs(4326).copy()
    final_junctions_wgs84["FLAW"] = final_junctions_wgs84.get("NODETYPE", "JUNCTION")

    map_path = args.outdir / "before_after_map.html"
    build_html_map(
        raw_wgs84, final_junctions_wgs84,
        conduit_color_field="FLAW", conduit_colors={"REMOVED": "#d7263d", "OK": "#8fb8ae"},
        junction_color_field="FLAW", junction_colors={"OUTFALL": "#d7263d", "JUNCTION": "#7d8f8a"},
        output_path=map_path, title="Drainage network - before / after cleaning",
    )
    print(f"Map written to: {map_path}")


if __name__ == "__main__":
    main()
