#!/usr/bin/env python
"""
Interactive editor for a Conduit/Junctions network.

Starts a small local web server (standard library only, bound to 127.0.0.1),
opens a map in the browser with every flaw from identify_flaws.py highlighted,
and lets you fix what you see:

    - click a legend entry to highlight one flaw type, tick "flaws only" to hide the rest
    - click a conduit or junction to open all of its attributes
    - edit any attribute, rename a junction (conduits that use the name follow)
    - delete or restore a conduit or junction
    - drag a selected junction to move it (conduit ends follow)
    - search a feature by NAME

Nothing is written until you press "Save". Save writes new files into --outdir
(conduit_edited.shp, junctions_edited.shp, edit_log.csv). The input shapefiles
are never modified. Closing the browser tab without saving discards the edits.

Usage:
    python edit_network.py --conduit path/to/Conduit.shp --junctions path/to/Junctions.shp
    python edit_network.py --conduit ... --junctions ... --outdir somewhere --port 8765

Run it on the output of clean_network.py to review and hand-fix what the
automatic cleaning left, or on the raw network to fix it by hand.
"""

from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

import network_checks as checks
import network_repairs as repairs
from identify_flaws import CONDUIT_COLORS, JUNCTION_COLORS, flag_conduits, flag_junctions

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR.parent / "outputs" / "edit"

# Level problems from network_repairs.py, shown only where no topology flaw already applies.
CONDUIT_COLORS = {**CONDUIT_COLORS, "UPHILL": "#e91e8c"}
JUNCTION_COLORS = {**JUNCTION_COLORS, "PIPETALL": "#1e88e5"}
FLAW_HELP = {
    "UPHILL": "outlet more than 0.5 m above the inlet",
    "PIPETALL": "closed pipe taller than rim minus invert",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Highlight and edit a drainage network on a map.")
    parser.add_argument("--conduit", type=Path, required=True, help="Path to the Conduit line shapefile.")
    parser.add_argument("--junctions", type=Path, required=True, help="Path to the Junctions point shapefile.")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR, help="Where Save writes the edited shapefiles and edit log.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_network(conduit_path: Path, junctions_path: Path):
    conduit = gpd.read_file(conduit_path)
    junctions = gpd.read_file(junctions_path)
    simulated_clean, _ = checks.simulate_clean_conduits(conduit)
    conduit, _, _ = flag_conduits(conduit)
    junctions, _ = flag_junctions(junctions, simulated_clean)

    uphill = repairs.find_uphill_conduits(conduit, junctions)
    tall = repairs.find_pipe_taller_than_node(conduit, junctions)
    conduit.loc[(conduit["FLAW"] == "OK") & conduit["NAME"].isin(uphill), "FLAW"] = "UPHILL"
    junctions.loc[(junctions["FLAW"] == "OK") & junctions["NAME"].isin(tall), "FLAW"] = "PIPETALL"
    return conduit, junctions


def to_geojson(gdf) -> dict:
    out = gdf.copy()
    out["_id"] = range(len(out))
    return json.loads(out.to_crs(4326).to_json(drop_id=True))


def _cast(value, dtype):
    """Form values arrive as text; turn them back into the column's type ('' becomes missing)."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return np.nan if pd.api.types.is_numeric_dtype(dtype) else None
    if pd.api.types.is_integer_dtype(dtype):
        return int(float(value))
    if pd.api.types.is_float_dtype(dtype):
        return float(value)
    return str(value)


def apply_edits(conduit, junctions, state: dict):
    """Apply the staged edits to copies of both layers. Returns (conduit, junctions, log rows)."""
    cd, jn = conduit.copy(), junctions.copy()
    log: list[dict] = []
    crs = jn.crs

    # Junction attribute edits and renames
    for key, fields in state.get("junction_edits", {}).items():
        i = int(key)
        for field, value in fields.items():
            old = jn.at[i, field]
            new = _cast(value, jn[field].dtype)
            jn.at[i, field] = new
            log.append({"layer": "junction", "name": jn.at[i, "NAME"] if field != "NAME" else new,
                        "field": field, "old": old, "new": new})
            if field == "NAME" and old != new:
                for col in ("INLETNODE", "OUTLETNODE"):
                    hit = cd[col] == old
                    if hit.any():
                        cd.loc[hit, col] = new
                        log.append({"layer": "conduit", "name": f"{int(hit.sum())} conduit(s)", "field": col, "old": old, "new": new})

    # Junction moves (browser sends lon/lat); attached conduit ends follow
    moved = state.get("junction_moved", {})
    if moved:
        pts = gpd.GeoSeries([Point(lon, lat) for lon, lat in moved.values()], crs=4326).to_crs(crs)
        for (key, _), pt in zip(moved.items(), pts):
            i = int(key)
            old_pt = jn.at[i, "geometry"]
            name = jn.at[i, "NAME"]
            jn.at[i, "geometry"] = pt
            for col in ("X", "Y"):
                if col in jn:
                    jn.at[i, col] = round(pt.x if col == "X" else pt.y, 3)
            for ci in cd.index[(cd["INLETNODE"] == name) | (cd["OUTLETNODE"] == name)]:
                coords = list(cd.at[ci, "geometry"].coords)
                if cd.at[ci, "INLETNODE"] == name:
                    coords[0] = (pt.x, pt.y)
                if cd.at[ci, "OUTLETNODE"] == name:
                    coords[-1] = (pt.x, pt.y)
                cd.at[ci, "geometry"] = LineString(coords)
            log.append({"layer": "junction", "name": name, "field": "geometry",
                        "old": f"{old_pt.x:.2f},{old_pt.y:.2f}", "new": f"{pt.x:.2f},{pt.y:.2f}"})

    # Conduit attribute edits
    for key, fields in state.get("conduit_edits", {}).items():
        i = int(key)
        for field, value in fields.items():
            old = cd.at[i, field]
            new = _cast(value, cd[field].dtype)
            cd.at[i, field] = new
            log.append({"layer": "conduit", "name": cd.at[i, "NAME"] if field != "NAME" else new,
                        "field": field, "old": old, "new": new})

    # Deletions last, so the indices used above stay valid
    for layer, gdf, key in (("conduit", cd, "conduit_deleted"), ("junction", jn, "junction_deleted")):
        ids = [int(k) for k in state.get(key, [])]
        for i in ids:
            log.append({"layer": layer, "name": gdf.at[i, "NAME"], "field": "(feature)", "old": "present", "new": "deleted"})
        gdf.drop(index=ids, inplace=True)
    return cd, jn, log


def save_edits(conduit, junctions, state: dict, outdir: Path) -> dict:
    cd, jn, log = apply_edits(conduit, junctions, state)
    outdir.mkdir(parents=True, exist_ok=True)
    cd.drop(columns=["FLAW"], errors="ignore").to_file(outdir / "conduit_edited.shp")
    jn.drop(columns=["FLAW"], errors="ignore").to_file(outdir / "junctions_edited.shp")
    pd.DataFrame(log, columns=["layer", "name", "field", "old", "new"]).to_csv(outdir / "edit_log.csv", index=False)
    return {"conduits": len(cd), "junctions": len(jn), "changes": len(log), "outdir": str(outdir.resolve())}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def make_handler(conduit, junctions, outdir: Path):
    payload = json.dumps({
        "conduit": to_geojson(conduit), "junctions": to_geojson(junctions),
        "conduit_colors": CONDUIT_COLORS, "junction_colors": JUNCTION_COLORS, "help": FLAW_HELP,
        "numeric": {"conduit": [c for c in conduit.columns if pd.api.types.is_numeric_dtype(conduit[c]) and c != "geometry"],
                    "junctions": [c for c in junctions.columns if pd.api.types.is_numeric_dtype(junctions[c]) and c != "geometry"]},
    }, allow_nan=False)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._send(200, payload.encode("utf-8"), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/save":
                self._send(404, b"not found", "text/plain")
                return
            try:
                state = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = save_edits(conduit, junctions, state, outdir)
                self._send(200, json.dumps(result).encode(), "application/json")
            except Exception as exc:  # report to the page instead of dropping the connection
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")

        def log_message(self, *args):
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Drainage network editor</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
 html,body{margin:0;height:100%;font:13px/1.45 -apple-system,Segoe UI,Arial,sans-serif;color:#222}
 #app{display:flex;height:100%} #map{flex:1} #side{width:340px;overflow:auto;border-right:1px solid #ccc;padding:10px 12px;box-sizing:border-box;background:#fafafa}
 h1{font-size:15px;margin:0 0 6px} h2{font-size:13px;margin:12px 0 4px;border-bottom:1px solid #ddd;padding-bottom:2px}
 .cat{display:flex;align-items:center;gap:6px;padding:2px 4px;cursor:pointer;border-radius:3px} .cat:hover{background:#eaeaea} .cat.on{background:#dde8f7}
 .sw{width:11px;height:11px;border-radius:2px;flex:none} .cat .n{margin-left:auto;color:#666}
 .muted{color:#666;font-size:12px} input[type=text]{width:100%;box-sizing:border-box;padding:3px 5px}
 table{width:100%;border-collapse:collapse} td{padding:1px 2px;vertical-align:middle} td.k{width:42%;color:#555;font-size:12px;word-break:break-all}
 td input{width:100%;box-sizing:border-box;padding:2px 4px;font-size:12px} td input.chg{background:#fff3b0}
 button{margin:4px 4px 0 0;padding:4px 9px;cursor:pointer} button.primary{background:#1565c0;color:#fff;border:0;border-radius:3px}
 button.danger{background:#c62828;color:#fff;border:0;border-radius:3px} #msg{margin-top:6px;font-size:12px}
 .more{max-height:46vh;overflow:auto}
</style></head><body><div id="app"><div id="side">
 <h1>Drainage network editor</h1>
 <div class="muted">Click a legend row to highlight a flaw. Click a feature to edit it. Nothing is written until Save.</div>
 <h2>Find</h2><input type="text" id="q" placeholder="NAME, then Enter">
 <h2>Conduit flaws</h2><div id="cc"></div>
 <h2>Junction flaws</h2><div id="jc"></div>
 <label class="muted"><input type="checkbox" id="only"> flaws only (hide OK features)</label>
 <h2>Selected</h2><div id="sel" class="muted">Nothing selected.</div>
 <h2>Edits</h2><div id="stat" class="muted"></div>
 <button class="primary" id="save">Save to files</button><button id="clear">Discard all edits</button>
 <div id="msg"></div>
</div><div id="map"></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const S={conduit_edits:{},junction_edits:{},junction_moved:{},conduit_deleted:new Set(),junction_deleted:new Set()};
let D,map,cl={},jl={},active=null,sel=null,dragger=null,cat={c:null,j:null};
const $=id=>document.getElementById(id);
fetch('/api/data').then(r=>r.json()).then(init);

function init(d){D=d;
 map=L.map('map',{preferCanvas:true,zoomSnap:0.25});
 L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,maxNativeZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
 const cg=L.featureGroup(),jg=L.featureGroup();
 D.conduit.features.forEach(f=>{const id=f.properties._id;
  const l=L.geoJSON(f,{style:()=>cs(f)}).getLayers()[0];l.on('click',e=>{L.DomEvent.stopPropagation(e);pick('c',id)});cl[id]=l;cg.addLayer(l)});
 D.junctions.features.forEach(f=>{const id=f.properties._id,[x,y]=f.geometry.coordinates;
  const l=L.circleMarker([y,x],js(f));l.on('click',e=>{L.DomEvent.stopPropagation(e);pick('j',id)});jl[id]=l;jg.addLayer(l)});
 cg.addTo(map);jg.addTo(map);map.fitBounds(cg.getBounds().extend(jg.getBounds()));
 legend('cc',D.conduit,D.conduit_colors,'c');legend('jc',D.junctions,D.junction_colors,'j');
 $('only').onchange=restyle;$('q').onkeydown=e=>{if(e.key==='Enter')find(e.target.value.trim())};
 $('save').onclick=save;$('clear').onclick=()=>{if(confirm('Discard every staged edit?')){reset();}};
 restyle();stat();}

function legend(el,fc,colors,k){const n={};fc.features.forEach(f=>n[f.properties.FLAW]=(n[f.properties.FLAW]||0)+1);
 $(el).innerHTML='';Object.keys(colors).filter(c=>n[c]).forEach(c=>{const r=document.createElement('div');r.className='cat';
  r.innerHTML=`<span class="sw" style="background:${colors[c]}"></span><span title="${D.help[c]||''}">${c}</span><span class="n">${n[c]}</span>`;
  r.onclick=()=>{cat[k]=cat[k]===c?null:c;active=cat[k]?k:(cat.c||cat.j?(cat.c?'c':'j'):null);document.querySelectorAll('.cat').forEach(x=>x.classList.remove('on'));
   ['c','j'].forEach(q=>{if(cat[q]){[...$(q==='c'?'cc':'jc').children].find(x=>x.children[1].textContent===cat[q]).classList.add('on')}});restyle();zoomCat(k,c)};
  $(el).appendChild(r)})}

function zoomCat(k,c){if(!cat[k])return;const ls=(k==='c'?D.conduit:D.junctions).features.filter(f=>f.properties.FLAW===c).map(f=>k==='c'?cl[f.properties._id]:jl[f.properties._id]);
 if(ls.length)map.fitBounds(L.featureGroup(ls).getBounds().pad(.2),{maxZoom:17})}

function dim(k,f){const hl=cat.c||cat.j;if(!hl)return false;return !(cat[k]&&f.properties.FLAW===cat[k])}
function cs(f){const p=f.properties,id=p._id,ok=p.FLAW==='OK',del=S.conduit_deleted.has(id),ed=S.conduit_edits[id];
 const hide=$('only')&&$('only').checked&&ok;
 return del?{color:'#777',weight:2,dashArray:'4 5',opacity:hide?0:.5}:{color:ed?'#00acc1':(D.conduit_colors[p.FLAW]||'#999'),weight:sel&&sel[0]==='c'&&sel[1]===id?7:(ok?1.6:3.5),opacity:hide?0:(dim('c',f)?.12:(ok?.6:.95))}}
function js(f){const p=f.properties,id=p._id,ok=p.FLAW==='OK',del=S.junction_deleted.has(id),ed=S.junction_edits[id]||S.junction_moved[id];
 const hide=$('only')&&$('only').checked&&ok,c=del?'#777':(ed?'#00acc1':(D.junction_colors[p.FLAW]||'#999'));
 const s=sel&&sel[0]==='j'&&sel[1]===id;
 return {radius:s?9:(ok?2.2:5),color:s?'#000':c,fillColor:c,fillOpacity:hide?0:(dim('j',f)?.1:.9),opacity:hide?0:(dim('j',f)?.1:1),weight:s?2:1}}
function restyle(){D.conduit.features.forEach(f=>cl[f.properties._id].setStyle(cs(f)));D.junctions.features.forEach(f=>jl[f.properties._id].setStyle(js(f)))}
function feat(k,id){return (k==='c'?D.conduit:D.junctions).features[id]}

function find(q){if(!q)return;const u=q.toUpperCase();
 for(const k of['c','j']){const f=(k==='c'?D.conduit:D.junctions).features.find(f=>String(f.properties.NAME).toUpperCase()===u);
  if(f){pick(k,f.properties._id);map.fitBounds(k==='c'?cl[f.properties._id].getBounds().pad(2):L.latLngBounds([jl[f.properties._id].getLatLng()]).pad(.01),{maxZoom:19});return}}
 msg('No feature named '+q)}

function pick(k,id){if(dragger){map.removeLayer(dragger);dragger=null}
 sel=[k,id];restyle();const f=feat(k,id),p=f.properties,ed=(k==='c'?S.conduit_edits:S.junction_edits)[id]||{};
 const del=(k==='c'?S.conduit_deleted:S.junction_deleted).has(id);
 let h=`<b>${k==='c'?'Conduit':'Junction'} ${p.NAME}</b> <span class="muted">flaw: ${p.FLAW}${del?' — marked for deletion':''}</span>`;
 h+='<div class="more"><table>'+Object.keys(p).filter(x=>x!=='_id'&&x!=='FLAW').map(x=>{const v=ed[x]!==undefined?ed[x]:p[x];
  return `<tr><td class="k">${x}</td><td><input data-f="${x}" class="${ed[x]!==undefined?'chg':''}" value="${v===null?'':String(v).replace(/"/g,'&quot;')}"></td></tr>`}).join('')+'</table></div>';
 h+=`<button class="primary" id="ap">Apply to feature</button><button id="rv">Revert</button><button class="danger" id="dl">${del?'Restore':'Delete'}</button>`;
 if(k==='j')h+='<div class="muted">Drag the black ring to move this junction.</div>';
 $('sel').className='';$('sel').innerHTML=h;
 $('ap').onclick=()=>applyForm(k,id);$('rv').onclick=()=>{delete (k==='c'?S.conduit_edits:S.junction_edits)[id];if(k==='j')delete S.junction_moved[id];refresh(k,id)};
 $('dl').onclick=()=>{const s=k==='c'?S.conduit_deleted:S.junction_deleted;s.has(id)?s.delete(id):s.add(id);
  if(k==='j'&&s.has(id)){const n=p.NAME,u=D.conduit.features.filter(c=>!S.conduit_deleted.has(c.properties._id)&&(c.properties.INLETNODE===n||c.properties.OUTLETNODE===n)).length;if(u)msg(u+' conduit(s) still use this junction')}
  refresh(k,id)};
 if(k==='j'){const ll=S.junction_moved[id]?[S.junction_moved[id][1],S.junction_moved[id][0]]:[f.geometry.coordinates[1],f.geometry.coordinates[0]];
  dragger=L.marker(ll,{draggable:true,icon:L.divIcon({className:'',html:'<div style="width:22px;height:22px;border:3px solid #000;border-radius:50%;margin:-11px 0 0 -11px"></div>',iconSize:[0,0]})}).addTo(map);
  dragger.on('dragend',e=>{const q=e.target.getLatLng();S.junction_moved[id]=[q.lng,q.lat];jl[id].setLatLng(q);refresh(k,id,true)})}}

function applyForm(k,id){const p=feat(k,id).properties,tgt=k==='c'?S.conduit_edits:S.junction_edits,num=new Set(D.numeric[k==='c'?'conduit':'junctions']);const e=tgt[id]||{};
 document.querySelectorAll('#sel input[data-f]').forEach(i=>{const f=i.dataset.f,cur=p[f]===null?'':String(p[f]);
  if(i.value!==cur){if(num.has(f)&&i.value.trim()!==''&&isNaN(Number(i.value))){msg(f+' must be a number');return}e[f]=i.value}else delete e[f]});
 if(Object.keys(e).length)tgt[id]=e;else delete tgt[id];refresh(k,id)}
function refresh(k,id,keep){stat();restyle();if(!keep)pick(k,id)}
function stat(){$('stat').textContent=`${Object.keys(S.conduit_edits).length} conduit edit(s), ${Object.keys(S.junction_edits).length} junction edit(s), ${Object.keys(S.junction_moved).length} moved, ${S.conduit_deleted.size} conduit(s) and ${S.junction_deleted.size} junction(s) marked for deletion`}
function msg(t){$('msg').textContent=t}
function reset(){S.conduit_edits={};S.junction_edits={};S.junction_moved={};S.conduit_deleted.clear();S.junction_deleted.clear();
 D.junctions.features.forEach(f=>{const[x,y]=f.geometry.coordinates;jl[f.properties._id].setLatLng([y,x])});
 if(dragger){map.removeLayer(dragger);dragger=null}sel=null;$('sel').textContent='Nothing selected.';stat();restyle();msg('')}
function save(){const b={conduit_edits:S.conduit_edits,junction_edits:S.junction_edits,junction_moved:S.junction_moved,
  conduit_deleted:[...S.conduit_deleted],junction_deleted:[...S.junction_deleted]};msg('Saving...');
 fetch('/api/save',{method:'POST',body:JSON.stringify(b)}).then(r=>r.json()).then(r=>msg(r.error?('Error: '+r.error):`Saved ${r.conduits} conduits, ${r.junctions} junctions, ${r.changes} change(s) to ${r.outdir}`))}
</script></body></html>
"""


def main() -> None:
    args = parse_args()
    for path in (args.conduit, args.junctions):
        if not path.exists():
            raise FileNotFoundError(f"Shapefile not found: {path}")
    print("Reading and checking the network...")
    conduit, junctions = load_network(args.conduit, args.junctions)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(conduit, junctions, args.outdir))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{len(conduit)} conduits, {len(junctions)} junctions. Editor running at {url}")
    print(f"Save writes to {args.outdir}. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
