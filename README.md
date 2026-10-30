# drainage_cleaning

Standalone Python tools that scan a SWMM-style drainage network (Conduit +
Junctions shapefiles) for topology flaws - dangling pipes, duplicate geometry,
unreferenced junctions, dead ends, redundant loops - color-code them, and produce
a fully cleaned network with an interactive map, and an editor to fix the rest by hand.

Two scripts for taking a raw SWMM-style drainage network (a Conduit line
shapefile + a Junctions point shapefile) and turning it into something that
doesn't fall over when you try to model it. Built against the Chirag Delhi
network but nothing in here is hardcoded to that dataset - point it at any
Conduit/Junctions pair with the same field layout (NAME, INLETNODE,
OUTLETNODE, XSECTION, INVERTELEV, etc.) and it works the same way.

Raw drainage exports like this always have the same handful of problems:
pipes that dangle off into nowhere, the same physical pipe digitized twice
under two different names, junctions nobody references anymore, dead ends
that were never converted to a proper outfall, and the odd redundant loop
where someone traced a branch twice. Going through 3,000+ features by hand
in QGIS to find these is how you lose a week. This finds them in a few
seconds and, if you want, fixes them too.

## The three tools

**`identify_flaws.py`** - read-only. Scans the network, flags every problem
it finds, writes a color-coded shapefile plus an interactive map. Doesn't
touch or fix anything. Run this first.

**`clean_network.py`** - does the actual work. Runs the same checks, then
removes/fixes what needs removing/fixing, and writes out a cleaned
Conduit/Junctions pair you can actually use.

**`edit_network.py`** - interactive editor. Opens a map in the browser with
every flaw highlighted, and lets you fix what you see by hand: edit any
attribute, rename a junction, move it, delete or restore a conduit or
junction. Use it on the raw network, or on the output of `clean_network.py`
to hand-fix what the automatic steps left.

All scripts read straight from your original shapefiles and never write
back to them - everything lands in `outputs/`, which is gitignored.

## What gets checked

Conduits:
- **ISLAND** - this conduit's connected cluster never reaches the main network
- **ORPHANEND** - nothing continues downstream from this conduit's outlet
- **DUPNAME** - another conduit has the exact same NAME
- **DUPGEOM** - another conduit connects the same two nodes (same pipe, two records)
- **SELFLOOP** - INLETNODE equals OUTLETNODE
- **BADGEOM** - invalid or zero-length geometry
- **NODIM** - no cross-section dimension was ever recorded
- **SLOPE999** - SLOPE is the -999 placeholder value, not a real number
- **NEGSLOPE** - the pipe runs uphill (outlet higher than inlet)
- **UPHILL** - (editor only) outlet more than 0.5 m above the inlet, found from the junction inverts

Junctions:
- **DUPNAME** - another junction has the exact same NAME
- **DUPLOC** - another junction sits at the same coordinate under a different NAME
- **BADGEOM** - invalid or empty point geometry
- **NOTREFD** - no surviving conduit references this junction anymore
- **PIPETALL** - (editor only) a closed pipe is taller than rim minus invert, so it does not fit the node

A feature only gets one flaw label even if it trips more than one check -
whichever's listed first above wins, since that's roughly most-fundamental-first.

## What clean_network.py actually does about it

1. Drops every dangling conduit (ISLAND + ORPHANEND).
2. For each duplicate-geometry pair, keeps one and drops the other. The
   pick is automatic: prefer the record with a real cross-section
   dimension, then the one with a recorded length, then whichever NAME
   sorts first. This replaces what was originally a hand-reviewed
   decision for each pair - fine for six pairs on one dataset, not
   something you want to do by hand every time this runs on a new
   network. Every pick gets logged in `clean_report.txt` so you can see
   exactly what was kept and why.
3. Drops junctions nothing references anymore.
4. Drops duplicate-NAME junctions, but only when every attribute on the
   copies is identical. If two junctions share a name but aren't actually
   identical, it leaves both in place and logs it - that's a real naming
   collision that needs a rename, not something safe to auto-delete.
5. Converts every remaining true dead end into a proper Outfall (renamed
   with an `OST_` prefix, `NODETYPE` set accordingly). A plain Junction in
   SWMM assumes flow continues somewhere; a dead end that's still typed as
   a Junction is a modeling error waiting to happen.
6. Finds bifurcations where the two branches reconverge downstream *and*
   their destination junctions have matching elevation/depth values -
   that combination means it's the same path traced twice, not a real
   split - and collapses the redundant branch.

7. Fixes invert spikes and pits (`network_repairs.py`). One junction with a
   wrong invert makes the pipe next to it run uphill. Where a single node
   stands more than 0.5 m out of the line of its neighbours, its invert is
   interpolated between them by pipe length. The fix is kept only if no
   other pipe at that node then runs uphill and the new invert stays below
   the rim. When both ends of a pipe fit the pattern, or the uphill run
   covers several nodes, nothing is changed and the reason is written to
   `invert_fix_review.csv`. `SLOPE` is recomputed on the touched pipes.
8. Raises rims where a closed pipe is taller than rim minus invert. The rim
   goes to the pipe crown, or to the ground when `--dem` is given and the
   ground is at or above the crown. With a DEM, rims below the ground are
   raised to it too, except where the survey and the DEM differ by more
   than 3 m (a levelling error, not a wrong manhole: those rims are kept
   and marked `RIM_CONFL`). Rims are never lowered. Every change is in
   `rim_raise_review.csv`.

Steps 7 and 8 can be skipped with `--no-repairs`. The original value of
every changed field stays in an `*_ORIG` column (`INV_ORIG`, `RIM_ORIG`,
`SLOPE_ORIG`).

Nothing here invents data. Anything the script can't resolve safely (a
naming collision that isn't identical, an outfall name that would collide
with something that already exists) gets logged and left alone instead of
guessed at.

## Running it

```bash
pip install -r requirements.txt

# 1. find the flaws (read-only)
python scripts/identify_flaws.py --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp

# 2. clean automatically (add --dem to let step 8 raise rims to the ground)
python scripts/clean_network.py  --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp --dem path/to/dem.tif

# 3. look at it and fix by hand
python scripts/edit_network.py   --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp
```

`--outdir` changes where results go (default `outputs/<tool>`).

Each run produces:

```
outputs/identify/
  conduit_flagged.shp      flagged conduits, FLAW field, matching .qml style for QGIS
  junctions_flagged.shp    flagged junctions, same deal
  flaws_map.html           open this in a browser - pan, zoom, click a feature for its flaw
  flaw_report.txt          counts per category

outputs/clean/
  conduit_final.shp        cleaned conduits, colored by XSECTION in QGIS
  junctions_final.shp      cleaned junctions, colored by NODETYPE (Outfall vs Junction)
  before_after_map.html    raw network with everything removed highlighted in red
  invert_fix_review.csv    every uphill pipe checked in step 7, with the decision and reason
  rim_raise_review.csv     every rim changed in step 8
  clean_report.txt         a line-by-line log of every removal/rename and why

outputs/edit/              written only when you press Save in the editor
  conduit_edited.shp
  junctions_edited.shp
  edit_log.csv             one row per changed value: layer, name, field, old, new
```

The `.qml` files sit next to their shapefile and QGIS picks them up
automatically the moment you drag the `.shp` into a project - same
colors as the HTML map.

## Using the editor

`edit_network.py` starts a server on `127.0.0.1` only (nothing is exposed to
the network) and opens the map. It needs internet for the map tiles and
Leaflet.

| To do this | Do this |
|---|---|
| See one flaw type | Click its row in the legend. The map zooms to it and dims the rest |
| Hide everything that is fine | Tick "flaws only" |
| Open a feature | Click it on the map, or type its NAME in Find |
| Change a value | Edit the field, press Apply (changed fields turn yellow) |
| Rename a junction | Edit NAME. Conduits whose INLETNODE/OUTLETNODE used it follow |
| Move a junction | Select it and drag the black ring. Attached conduit ends follow |
| Delete / restore | Delete button. The feature turns grey and dashed and is removed on Save |
| Undo one feature | Revert |
| Write the result | Save to files. Nothing is written before this |

Edited features show in cyan. Closing the tab without saving discards the
edits. The input shapefiles are never changed. Not in the editor: drawing new
conduits or junctions.

## Layout

```
drainage_cleaning/
  scripts/
    network_checks.py   all the actual detection/repair logic - both
                         pipelines import from here so they can't
                         quietly disagree about what counts as a flaw
    network_repairs.py   invert-spike and rim repairs (steps 7 and 8) and the
                         UPHILL / PIPETALL detectors the editor uses
    mapping.py           builds the HTML maps and .qml style files
    identify_flaws.py    pipeline 1 - CLI entry point
    clean_network.py     pipeline 2 - CLI entry point
    edit_network.py      interactive map editor - local server + one HTML page
  outputs/                generated, gitignored
  requirements.txt
```

## A couple of things worth knowing before you trust the output blindly

- The duplicate-geometry pick in step 2 is a heuristic, not a certainty.
  It's the same logic a human reviewer used the first time this was done
  by hand on this dataset, just generalized - but "which of two identical
  records to keep" is sometimes genuinely arbitrary. Check
  `clean_report.txt` if it matters which specific record survived.
- NEGSLOPE conduits aren't auto-fixed - flat terrain can produce a
  legitimately tiny negative slope from survey noise, and telling that
  apart from an actual reversed pipe needs a real elevation comparison
  the pipeline doesn't attempt.
- Step 7 only fixes the clear case, one wrong node among right neighbours.
  On a raw export with placeholder inverts (0.0) most uphill pipes are
  rejected on purpose and listed in `invert_fix_review.csv` for you to
  correct in the editor.
- Step 8 changes the rim, never the pipe. A rim raised to the crown is a
  modeling convenience (SWMM would otherwise raise it silently at run
  time); look at the large raises in `rim_raise_review.csv`.
- Redundant-loop detection only fires when both branches reconverge
  within 10 hops and their destination junctions' elevation/depth match
  within 1cm. A real tributary split that happens to reconverge won't get
  collapsed unless the elevations also happen to line up - which is the
  point, but it means the check is intentionally conservative.
