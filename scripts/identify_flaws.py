#!/usr/bin/env python
"""
Pipeline 1 of 2: find every flaw in a Conduit/Junctions drainage
network and color-code them. Detection only - this script never
touches the input shapefiles and never removes anything. Run
clean_network.py once you've looked at what this finds.

Usage:
    python identify_flaws.py
    python identify_flaws.py --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp

With no arguments it runs against the Chirag Delhi shapefiles this
tool was built against (../../05_chirag_delhi/01_raw_drainage_network).

What it checks, conduits (highest-priority flaw wins if a feature
trips more than one):
    ISLAND     - this conduit's connected cluster never reaches the
                 main network
    ORPHANEND  - nothing continues downstream from this conduit's outlet
    DUPNAME    - another conduit shares the exact same NAME
    DUPGEOM    - another conduit connects the same two nodes (the same
                 physical pipe digitized twice)
    SELFLOOP   - INLETNODE equals OUTLETNODE
    BADGEOM    - invalid or zero-length geometry
    NODIM      - no cross-section dimension was ever recorded (GEOM1=0)
    SLOPE999   - SLOPE is the -999 placeholder, not a real value
    NEGSLOPE   - SLOPE is negative (outlet above inlet - backwards for
                 gravity flow, or flat-terrain survey noise)

What it checks, junctions:
    DUPNAME    - another junction shares the exact same NAME
    DUPLOC     - another junction sits at the exact same coordinate
                 under a different NAME
    BADGEOM    - invalid or empty point geometry
    NOTREFD    - not referenced by any conduit once dangling/duplicate
                 conduits are (hypothetically) removed - informational,
                 not necessarily wrong on its own

Output (outputs/identify/):
    conduit_flagged.shp / .qml   - every conduit, FLAW field added
    junctions_flagged.shp / .qml - every junction, FLAW field added
    flaws_map.html                - interactive map, pan/zoom/click
    flaw_report.txt               - counts per category
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd

import network_checks as checks
from mapping import build_html_map, write_qml_lines, write_qml_points

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR.parent / "outputs" / "identify"

CONDUIT_PRIORITY = ["ISLAND", "ORPHANEND", "DUPNAME", "DUPGEOM", "SELFLOOP", "BADGEOM", "NODIM", "SLOPE999", "NEGSLOPE"]
CONDUIT_COLORS = {
    "ISLAND": "#d7263d", "ORPHANEND": "#e8734a", "DUPNAME": "#f46d43", "DUPGEOM": "#a259d9",
    "SELFLOOP": "#f1c40f", "BADGEOM": "#6b3f27", "NODIM": "#8f8f00", "SLOPE999": "#5b4636",
    "NEGSLOPE": "#f4a300", "OK": "#8fb8ae",
}

JUNCTION_PRIORITY = ["DUPNAME", "DUPLOC", "BADGEOM", "NOTREFD"]
JUNCTION_COLORS = {
    "DUPNAME": "#f46d43", "DUPLOC": "#a259d9", "BADGEOM": "#6b3f27", "NOTREFD": "#4fa3c7", "OK": "#8fb8ae",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect and color-code every flaw in a drainage network.")
    parser.add_argument("--conduit", type=Path, required=True, help="Path to the Conduit line shapefile.")
    parser.add_argument("--junctions", type=Path, required=True, help="Path to the Junctions point shapefile.")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR, help="Where to write flagged shapefiles, map, and report.")
    return parser.parse_args()


def flag_conduits(conduit) -> tuple:
    island_sizes = checks.find_disconnected_islands(conduit)
    island_names = set(island_sizes.keys())
    orphan_names = checks.find_orphan_ends(conduit) - island_names

    dup_name_groups = checks.find_duplicate_names(conduit, "NAME")
    dupname_idx = {i for idxs in dup_name_groups.values() for i in idxs}
    dupname_names = set(conduit.loc[list(dupname_idx), "NAME"]) if dupname_idx else set()

    dupgeom_partners = checks.find_duplicate_node_pairs(conduit)
    dupgeom_names = set(dupgeom_partners.keys())

    selfloop_names = checks.find_self_loops(conduit)
    bad_geom = checks.find_bad_geometries(conduit, is_line=True)
    badgeom_names = set(conduit.loc[list(bad_geom["invalid"] | bad_geom["zero_length"]), "NAME"])

    attr_faults = checks.find_attribute_faults(conduit)

    per_name_flags = {
        "ISLAND": island_names, "ORPHANEND": orphan_names, "DUPNAME": dupname_names,
        "DUPGEOM": dupgeom_names, "SELFLOOP": selfloop_names, "BADGEOM": badgeom_names,
        "NODIM": attr_faults["NODIM"], "SLOPE999": attr_faults["SLOPE999"], "NEGSLOPE": attr_faults["NEGSLOPE"],
    }

    flaw_col = []
    for name in conduit["NAME"]:
        flaw = next((cat for cat in CONDUIT_PRIORITY if name in per_name_flags[cat]), "OK")
        flaw_col.append(flaw)
    conduit = conduit.copy()
    conduit["FLAW"] = flaw_col

    counts = {cat: sum(1 for f in flaw_col if f == cat) for cat in CONDUIT_PRIORITY + ["OK"]}
    return conduit, counts, {"island_sizes": island_sizes, "dupgeom_partners": dupgeom_partners}


def flag_junctions(junctions, clean_conduit_for_notrefd) -> tuple:
    dup_name_groups = checks.find_duplicate_names(junctions, "NAME")
    dupname_idx = {i for idxs in dup_name_groups.values() for i in idxs}
    dupname_names = set(junctions.loc[list(dupname_idx), "NAME"]) if dupname_idx else set()

    duploc_partners = checks.find_duplicate_locations(junctions)
    duploc_names = set(duploc_partners.keys())

    bad_geom = checks.find_bad_geometries(junctions, is_line=False)
    badgeom_names = set(junctions.loc[list(bad_geom["invalid"]), "NAME"])

    notrefd_names = checks.find_unreferenced_junctions(junctions, clean_conduit_for_notrefd)

    per_name_flags = {"DUPNAME": dupname_names, "DUPLOC": duploc_names, "BADGEOM": badgeom_names, "NOTREFD": notrefd_names}

    flaw_col = []
    for name in junctions["NAME"]:
        flaw = next((cat for cat in JUNCTION_PRIORITY if name in per_name_flags[cat]), "OK")
        flaw_col.append(flaw)
    junctions = junctions.copy()
    junctions["FLAW"] = flaw_col

    counts = {cat: sum(1 for f in flaw_col if f == cat) for cat in JUNCTION_PRIORITY + ["OK"]}
    return junctions, counts


def write_report(
    path: Path, conduit_counts: dict, junction_counts: dict, n_conduit: int, n_junctions: int,
    geometric_dangle_count: int,
) -> None:
    lines = [
        "Drainage Network - Flaw Report",
        "=" * 60,
        f"Conduits scanned:  {n_conduit}",
        f"Junctions scanned: {n_junctions}",
        "",
        "Conduit flaws (priority order - a feature is counted once, under its highest-priority flaw):",
    ]
    for category, count in conduit_counts.items():
        lines.append(f"  {category:<10} {count}")
    lines += ["", "Junction flaws:"]
    for category, count in junction_counts.items():
        lines.append(f"  {category:<10} {count}")
    lines += [
        "",
        "Extra, geometry-only check (not folded into FLAW - purely informational):",
        f"  Conduits with an endpoint more than {checks.SNAP_TOLERANCE_M} m from any junction point: {geometric_dangle_count}",
        "  (Attribute-based ISLAND/ORPHANEND above is what the clean pipeline actually acts on;",
        "   this catches a digitizing gap even where INLETNODE/OUTLETNODE look internally consistent.)",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()

    if not args.conduit.exists():
        raise FileNotFoundError(f"Conduit shapefile not found: {args.conduit}")
    if not args.junctions.exists():
        raise FileNotFoundError(f"Junctions shapefile not found: {args.junctions}")

    print(f"Reading {args.conduit}")
    conduit = gpd.read_file(args.conduit)
    print(f"Reading {args.junctions}")
    junctions = gpd.read_file(args.junctions)
    print(f"{len(conduit)} conduits, {len(junctions)} junctions")

    print("Checking conduits (islands, orphan ends, duplicates, self-loops, geometry, attributes)...")
    flagged_conduit, conduit_counts, _extra = flag_conduits(conduit)

    print("Simulating a cleaned conduit set to test which junctions would still be referenced...")
    simulated_clean, sim_log = checks.simulate_clean_conduits(conduit)
    for line in sim_log:
        print(f"  {line}")

    print("Checking junctions (duplicates, geometry, references)...")
    flagged_junctions, junction_counts = flag_junctions(junctions, simulated_clean)

    args.outdir.mkdir(parents=True, exist_ok=True)

    conduit_out = args.outdir / "conduit_flagged.shp"
    junctions_out = args.outdir / "junctions_flagged.shp"
    flagged_conduit.to_file(conduit_out)
    flagged_junctions.to_file(junctions_out)
    write_qml_lines(conduit_out.with_suffix(".qml"), "FLAW", {k: v for k, v in CONDUIT_COLORS.items()})
    write_qml_points(junctions_out.with_suffix(".qml"), "FLAW", {k: v for k, v in JUNCTION_COLORS.items()})
    print(f"Flagged shapefiles written to: {args.outdir}")

    print("Checking geometric dangles (endpoint-to-nearest-junction distance)...")
    geometric_dangles = checks.find_dangling_by_geometry(conduit, junctions)

    report_path = args.outdir / "flaw_report.txt"
    write_report(report_path, conduit_counts, junction_counts, len(conduit), len(junctions), len(geometric_dangles))
    print(f"Report written to: {report_path}")

    print("Building interactive map...")
    conduit_wgs84 = flagged_conduit.to_crs(4326)
    junctions_wgs84 = flagged_junctions.to_crs(4326)
    map_path = args.outdir / "flaws_map.html"
    build_html_map(
        conduit_wgs84, junctions_wgs84,
        conduit_color_field="FLAW", conduit_colors=CONDUIT_COLORS,
        junction_color_field="FLAW", junction_colors=JUNCTION_COLORS,
        output_path=map_path, title="Drainage network - flaws found",
    )
    print(f"Map written to: {map_path}")

    print()
    print("Conduit flaw counts:")
    for category, count in conduit_counts.items():
        print(f"  {category:<10} {count}")
    print("Junction flaw counts:")
    for category, count in junction_counts.items():
        print(f"  {category:<10} {count}")


if __name__ == "__main__":
    main()
