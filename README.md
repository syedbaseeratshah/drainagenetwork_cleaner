# drainage_cleaning

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

## The two pipelines

**`identify_flaws.py`** - read-only. Scans the network, flags every problem
it finds, writes a color-coded shapefile plus an interactive map. Doesn't
touch or fix anything. Run this first.

**`clean_network.py`** - does the actual work. Runs the same checks, then
removes/fixes what needs removing/fixing, and writes out a cleaned
Conduit/Junctions pair you can actually use.

Both scripts read straight from your original shapefiles and never write
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

Junctions:
- **DUPNAME** - another junction has the exact same NAME
- **DUPLOC** - another junction sits at the same coordinate under a different NAME
- **BADGEOM** - invalid or empty point geometry
- **NOTREFD** - no surviving conduit references this junction anymore

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

Nothing here invents data. Anything the script can't resolve safely (a
naming collision that isn't identical, an outfall name that would collide
with something that already exists) gets logged and left alone instead of
guessed at.

## Running it

```bash
pip install -r requirements.txt

# against the Chirag Delhi network (default paths, no arguments needed)
python scripts/identify_flaws.py
python scripts/clean_network.py

# against any other network
python scripts/identify_flaws.py --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp --outdir outputs/my_run
python scripts/clean_network.py   --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp --outdir outputs/my_run
```

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
  clean_report.txt         a line-by-line log of every removal/rename and why
```

The `.qml` files sit next to their shapefile and QGIS picks them up
automatically the moment you drag the `.shp` into a project - same
colors as the HTML map.

## Layout

```
drainage_cleaning/
  scripts/
    network_checks.py   all the actual detection/repair logic - both
                         pipelines import from here so they can't
                         quietly disagree about what counts as a flaw
    mapping.py           builds the HTML maps and .qml style files
    identify_flaws.py    pipeline 1 - CLI entry point
    clean_network.py     pipeline 2 - CLI entry point
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
- Redundant-loop detection only fires when both branches reconverge
  within 10 hops and their destination junctions' elevation/depth match
  within 1cm. A real tributary split that happens to reconverge won't get
  collapsed unless the elevations also happen to line up - which is the
  point, but it means the check is intentionally conservative.
