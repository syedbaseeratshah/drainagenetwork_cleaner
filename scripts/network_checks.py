"""
Shared detection and repair logic for a SWMM-style Conduit/Junctions
drainage network (the same data shape as the Chirag Delhi export:
Conduit lines with NAME/INLETNODE/OUTLETNODE/..., Junction points with
NAME/geometry/INVERTELEV/RIMELEV/DEPTH/...).

Both identify_flaws.py and clean_network.py import from here so the
two pipelines can never quietly disagree about what counts as a flaw.
identify_flaws.py only calls the find_* functions. clean_network.py
calls them too, then acts on what they return.

Everything here works off two plain GeoDataFrames (conduits, junctions)
and node names as strings - no dependency on any particular source
format, so it isn't tied to shapefiles or to this one dataset's field
list beyond the handful of columns it actually reads.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

# Node-name prefixes this dataset's SWMM export uses to tell node types
# apart (there's no separate "node type" column - it's baked into the
# name). A network using different prefixes only needs these changed.
JUNCTION_PREFIX = "JST_"
OUTFALL_PREFIX = "OST_"
STORAGE_PREFIX = "SST_"

# How close a conduit endpoint must be to a junction point to count as
# "connected" to it. Small tolerance to absorb digitizing/floating point
# noise, well under the shortest real conduit in this kind of dataset.
SNAP_TOLERANCE_M = 0.5

# A bifurcation is only ever auto-flagged as a redundant loop if its
# branches reconverge within this many hops downstream.
LOOP_TRACE_MAX_HOPS = 10

# Tolerance for treating two junctions' INVERTELEV/RIMELEV/DEPTH as
# "the same" rather than requiring an exact float match.
LOOP_ATTRIBUTE_TOLERANCE = 0.01


def norm(name) -> str:
    """Strip whitespace and force uppercase before any node-name comparison."""
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return ""
    return str(name).strip().upper()


def is_other_node_type(name: str) -> bool:
    """True for a Storage or Outfall node name - a different SWMM node type, not a plain junction."""
    return name.startswith(STORAGE_PREFIX) or name.startswith(OUTFALL_PREFIX)


# ---------------------------------------------------------------------------
# Conduit checks
# ---------------------------------------------------------------------------

def find_disconnected_islands(conduit) -> dict[str, int]:
    """
    Conduits are chained together by shared node names (INLETNODE/
    OUTLETNODE). Union-find over those names finds every connected
    component; anything not in the single largest one is an "island" -
    a cluster that never reaches the main network.

    Returns {conduit NAME: component size in conduits} for every
    conduit outside the main component.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for _, row in conduit.iterrows():
        inlet, outlet = norm(row["INLETNODE"]), norm(row["OUTLETNODE"])
        union(inlet, outlet)

    component_conduit_count: Counter = Counter()
    root_by_name: dict[str, str] = {}
    for _, row in conduit.iterrows():
        root = find(norm(row["INLETNODE"]))
        root_by_name[row["NAME"]] = root
        component_conduit_count[root] += 1

    if not component_conduit_count:
        return {}

    main_root = component_conduit_count.most_common(1)[0][0]
    return {
        name: component_conduit_count[root]
        for name, root in root_by_name.items()
        if root != main_root
    }


def find_orphan_ends(conduit) -> set[str]:
    """
    A conduit's OUTLETNODE name is never used as any OTHER conduit's
    INLETNODE - nothing continues downstream from here. Directed check:
    two pipes merely converging on the same dead-end point doesn't
    count. Storage/Outfall node names are excluded (a different node
    type, not a broken conduit).
    """
    out_degree: Counter = Counter()
    for _, row in conduit.iterrows():
        out_degree[norm(row["INLETNODE"])] += 1

    orphans = set()
    for _, row in conduit.iterrows():
        outlet = norm(row["OUTLETNODE"])
        if is_other_node_type(outlet):
            continue
        if out_degree[outlet] == 0:
            orphans.add(row["NAME"])
    return orphans


def find_duplicate_names(gdf, key: str = "NAME") -> dict[str, list[int]]:
    """Feature index groups sharing the same value in `key` field (more than one feature)."""
    groups: dict = defaultdict(list)
    for idx, value in gdf[key].items():
        groups[value].append(idx)
    return {name: idxs for name, idxs in groups.items() if len(idxs) > 1}


def find_duplicate_node_pairs(conduit) -> dict[str, list[str]]:
    """
    Conduits sharing the same (INLETNODE, OUTLETNODE) pair, direction
    independent, with a different NAME - two records connecting the
    same two points occupy the same line, i.e. the same physical pipe
    digitized twice under two names. Self-loops are excluded (their own
    category). Returns {conduit NAME: [other NAMEs sharing the pair]}.
    """
    by_pair: dict = defaultdict(list)
    for _, row in conduit.iterrows():
        inlet, outlet = norm(row["INLETNODE"]), norm(row["OUTLETNODE"])
        if not inlet or not outlet or inlet == outlet:
            continue
        by_pair[frozenset((inlet, outlet))].append(row["NAME"])

    result: dict[str, list[str]] = {}
    for names in by_pair.values():
        if len(names) < 2:
            continue
        for name in names:
            result[name] = [other for other in names if other != name]
    return result


def find_self_loops(conduit) -> set[str]:
    """Conduits whose INLETNODE equals OUTLETNODE - a pipe that starts and ends at the same node."""
    loops = set()
    for _, row in conduit.iterrows():
        inlet, outlet = norm(row["INLETNODE"]), norm(row["OUTLETNODE"])
        if inlet and outlet and inlet == outlet:
            loops.add(row["NAME"])
    return loops


def find_bad_geometries(gdf, is_line: bool) -> dict[str, set]:
    """Self-intersecting/empty geometries, plus zero-length lines. Keyed by row index."""
    invalid, zero_length = set(), set()
    for idx, geom in gdf["geometry"].items():
        if geom is None or geom.is_empty:
            invalid.add(idx)
            continue
        if not geom.is_valid:
            invalid.add(idx)
        if is_line and geom.length == 0:
            zero_length.add(idx)
    return {"invalid": invalid, "zero_length": zero_length}


def find_attribute_faults(conduit) -> dict[str, set]:
    """
    NODIM: GEOM1 == 0, no cross-section dimension was ever recorded.
    SLOPE999: SLOPE == -999, a placeholder/sentinel, not a real number.
    NEGSLOPE: SLOPE < 0 and not the -999 sentinel - outlet recorded
    higher than inlet, backwards for gravity flow (or flat-terrain
    survey noise - needs a real INVERTELEV comparison to be sure).
    """
    nodim, slope999, negslope = set(), set(), set()
    for _, row in conduit.iterrows():
        name = row["NAME"]
        geom1 = row.get("GEOM1")
        slope = row.get("SLOPE")
        if geom1 is not None and geom1 == 0:
            nodim.add(name)
        if slope is None:
            continue
        if abs(slope - (-999)) < 0.001:
            slope999.add(name)
        elif slope < 0:
            negslope.add(name)
    return {"NODIM": nodim, "SLOPE999": slope999, "NEGSLOPE": negslope}


def find_dangling_by_geometry(conduit, junctions, tolerance: float = SNAP_TOLERANCE_M) -> set[str]:
    """
    Conduit endpoints that don't land on any junction point within
    `tolerance`, using a coordinate spatial index rather than
    INLETNODE/OUTLETNODE text at all - catches a digitizing gap even
    when the attribute table looks internally consistent.
    """
    from shapely.strtree import STRtree

    junction_points = list(junctions.geometry)
    if not junction_points:
        return set(conduit["NAME"])
    tree = STRtree(junction_points)

    dangling = set()
    for _, row in conduit.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        coords = list(geom.coords)
        for endpoint in (coords[0], coords[-1]):
            from shapely.geometry import Point

            point = Point(endpoint)
            nearest_idx = tree.nearest(point)
            nearest = junction_points[nearest_idx]
            if point.distance(nearest) > tolerance:
                dangling.add(row["NAME"])
                break
    return dangling


# ---------------------------------------------------------------------------
# Junction checks
# ---------------------------------------------------------------------------

def find_duplicate_locations(junctions) -> dict[str, list[str]]:
    """
    Junctions sitting at the exact same coordinate (rounded to 2
    decimals, i.e. centimeter precision) but with different NAMEs - the
    same physical junction digitized twice under two names.
    """
    by_location: dict = defaultdict(list)
    for _, row in junctions.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        key = (round(geom.x, 2), round(geom.y, 2))
        by_location[key].append(row["NAME"])

    result: dict[str, list[str]] = {}
    for names in by_location.values():
        distinct = set(names)
        if len(distinct) < 2:
            continue
        for name in names:
            others = sorted(distinct - {name})
            if others:
                result[name] = others
    return result


def find_unreferenced_junctions(junctions, conduit) -> set[str]:
    """Junction NAMEs never used as any conduit's INLETNODE or OUTLETNODE."""
    referenced = set()
    for _, row in conduit.iterrows():
        referenced.add(norm(row["INLETNODE"]))
        referenced.add(norm(row["OUTLETNODE"]))
    return {row["NAME"] for _, row in junctions.iterrows() if norm(row["NAME"]) not in referenced}


# ---------------------------------------------------------------------------
# Simulated "clean" conduit set - used by identify_flaws.py purely to
# compute the informational NOTREFD junction flag, and used by
# clean_network.py as the actual first repair pass.
# ---------------------------------------------------------------------------

def resolve_duplicate_geometry_group(rows: list[dict]) -> tuple[dict, list[dict]]:
    """
    Given every conduit row sharing one (INLETNODE, OUTLETNODE) pair,
    deterministically pick one to keep and return (kept, removed).

    Preference order (automatic stand-in for a human review pass):
      1. Has a real cross-section dimension (GEOM1 != 0) over one that
         doesn't - an incomplete/abandoned digitizing entry.
      2. Has a non-null LENGTH over a missing one.
      3. Lexicographically smallest NAME - deterministic, no other
         basis available.
    """
    def sort_key(row):
        has_dim = 0 if (row.get("GEOM1") or 0) != 0 else 1
        has_length = 0 if row.get("LENGTH") not in (None, 0) else 1
        return (has_dim, has_length, str(row["NAME"]))

    ordered = sorted(rows, key=sort_key)
    return ordered[0], ordered[1:]


def simulate_clean_conduits(conduit) -> "tuple":
    """
    Apply the same two removal passes clean_network.py performs first
    (drop dangling conduits, then resolve DUPGEOM duplicates) purely in
    memory, without writing anything. Returns (clean_conduit_gdf, log)
    where log is a list of human-readable strings describing every
    automatic decision made - both pipelines print/save this so nothing
    happens silently.
    """
    log: list[str] = []

    island_names = set(find_disconnected_islands(conduit).keys())
    orphan_names = find_orphan_ends(conduit) - island_names
    dangling_names = island_names | orphan_names
    log.append(
        f"Dangling conduits removed: {len(dangling_names)} "
        f"(ISLAND: {len(island_names)}, ORPHANEND: {len(orphan_names)})"
    )

    step1 = conduit[~conduit["NAME"].isin(dangling_names)].copy()

    dup_pairs = find_duplicate_node_pairs(step1)
    groups: dict = defaultdict(list)
    for _, row in step1.iterrows():
        inlet, outlet = norm(row["INLETNODE"]), norm(row["OUTLETNODE"])
        if row["NAME"] in dup_pairs and inlet and outlet and inlet != outlet:
            groups[frozenset((inlet, outlet))].append(row.to_dict())

    remove_names = set()
    for pair_rows in groups.values():
        if len(pair_rows) < 2:
            continue
        kept, removed = resolve_duplicate_geometry_group(pair_rows)
        for row in removed:
            remove_names.add(row["NAME"])
            log.append(f"Duplicate geometry: removed {row['NAME']!r}, kept {kept['NAME']!r} (same pipe digitized twice)")

    step2 = step1[~step1["NAME"].isin(remove_names)].copy()
    log.append(f"Duplicate-geometry conduits removed: {len(remove_names)}")
    log.append(f"Conduits remaining after dangling + duplicate removal: {len(step2)} of {len(conduit)}")

    return step2, log


# ---------------------------------------------------------------------------
# Bifurcations and redundant loops - operate on an already-cleaned
# conduit/junction pair (post terminal-outfall conversion in the clean
# pipeline; on the simulated-clean set in the identify pipeline).
# ---------------------------------------------------------------------------

def find_bifurcations(conduit) -> dict[str, int]:
    """Junction NAME used as the INLETNODE of 2+ different conduits -> {NAME: branch count}."""
    downstream: dict = defaultdict(list)
    for _, row in conduit.iterrows():
        downstream[norm(row["INLETNODE"])].append(row["NAME"])
    return {node: len(names) for node, names in downstream.items() if len(names) > 1}


def _attributes_match(a: dict, b: dict, tolerance: float) -> bool:
    for key in ("INVERTELEV", "RIMELEV", "DEPTH"):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return False
        if abs(va - vb) > tolerance:
            return False
    return True


def _trace_branch(start_node: str, adjacency: dict, max_hops: int) -> tuple[list[str], list[str]]:
    """Follow a single unambiguous path forward until a dead end, another bifurcation, a cycle, or max_hops."""
    nodes = [start_node]
    conduits: list[str] = []
    current = start_node
    for _ in range(max_hops):
        edges = adjacency.get(current, [])
        if len(edges) != 1:
            break
        conduit_name, next_node = edges[0]
        if next_node in nodes:
            break
        conduits.append(conduit_name)
        nodes.append(next_node)
        current = next_node
    return nodes, conduits


def find_redundant_loops(
    conduit,
    junctions,
    max_hops: int = LOOP_TRACE_MAX_HOPS,
    tolerance: float = LOOP_ATTRIBUTE_TOLERANCE,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    A bifurcation (find_bifurcations) is only ever flagged as a
    redundant loop - the same path digitized twice, not a genuine
    tributary split - when BOTH hold: its branches reconverge at a
    common node within `max_hops`, AND the branches' immediate
    destination junctions have matching INVERTELEV/RIMELEV/DEPTH.
    Reconvergence alone isn't enough - two real, distinct branches
    merging downstream look topologically identical to a duplicate.

    Returns ({conduit NAME: loop_id}, {junction NAME: loop_id}) for the
    redundant branch only (the other branch of each pair is kept).
    """
    adjacency: dict = defaultdict(list)
    for _, row in conduit.iterrows():
        adjacency[norm(row["INLETNODE"])].append((row["NAME"], norm(row["OUTLETNODE"])))

    junction_attrs = {
        norm(row["NAME"]): {"INVERTELEV": row.get("INVERTELEV"), "RIMELEV": row.get("RIMELEV"), "DEPTH": row.get("DEPTH")}
        for _, row in junctions.iterrows()
    }

    redundant_conduits: dict[str, int] = {}
    redundant_junctions: dict[str, int] = {}
    loop_id = 0

    for node, branches in adjacency.items():
        if len(branches) < 2:
            continue
        for i in range(len(branches)):
            for j in range(i + 1, len(branches)):
                c1, d1 = branches[i]
                c2, d2 = branches[j]
                a1, a2 = junction_attrs.get(d1), junction_attrs.get(d2)
                if a1 is None or a2 is None or not _attributes_match(a1, a2, tolerance):
                    continue

                nodes1, conduits1 = _trace_branch(d1, adjacency, max_hops)
                nodes2, conduits2 = _trace_branch(d2, adjacency, max_hops)
                nodes2_set = set(nodes2)
                reconverge_idx1 = next((k for k, n in enumerate(nodes1) if n in nodes2_set), None)
                if reconverge_idx1 is None:
                    continue
                reconverge_node = nodes1[reconverge_idx1]
                reconverge_idx2 = nodes2.index(reconverge_node)

                # Deterministic pick of which branch is the redundant one.
                if c1 <= c2:
                    redundant_first, redundant_nodes, redundant_path, cut = c2, nodes2, conduits2, reconverge_idx2
                else:
                    redundant_first, redundant_nodes, redundant_path, cut = c1, nodes1, conduits1, reconverge_idx1

                loop_id += 1
                for name in [redundant_first] + redundant_path[:cut]:
                    redundant_conduits[name] = loop_id
                for name in redundant_nodes[:cut]:
                    redundant_junctions[name] = loop_id

    return redundant_conduits, redundant_junctions


def find_terminal_dead_ends(conduit) -> set[str]:
    """
    Every OUTLETNODE that nothing else picks up as an INLETNODE, i.e. a
    true dead end in an already-cleaned network - water leaves the
    modeled system here. Storage/Outfall names excluded (already a
    different node type).
    """
    out_degree: Counter = Counter()
    for _, row in conduit.iterrows():
        out_degree[norm(row["INLETNODE"])] += 1

    terminals = set()
    for _, row in conduit.iterrows():
        outlet = norm(row["OUTLETNODE"])
        if is_other_node_type(outlet):
            continue
        if out_degree[outlet] == 0:
            terminals.add(outlet)
    return terminals
