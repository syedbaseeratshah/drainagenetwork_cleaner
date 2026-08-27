"""
Turns a flagged Conduit/Junctions GeoDataFrame pair into visual output:
an interactive HTML map (folium/Leaflet, reprojected to WGS84 so it
sits on a real basemap) and a QGIS .qml style file (so the same
color-coding shows up automatically if the shapefile is opened in
QGIS, matching how the original pipeline styled its outputs).
"""

from __future__ import annotations

from pathlib import Path

import folium


def _category_html(category_field: str, colors: dict[str, str]) -> str:
    rows = "".join(
        f'<div><span style="display:inline-block;width:11px;height:11px;'
        f'background:{color};margin-right:6px;border-radius:2px;"></span>{label}</div>'
        for label, color in colors.items()
    )
    return f"""
    <div style="position:fixed;bottom:24px;left:24px;z-index:9999;background:white;
                padding:10px 14px;border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,0.3);
                font:13px/1.6 -apple-system,Segoe UI,Arial,sans-serif;color:#222;">
      <div style="font-weight:600;margin-bottom:4px;">{category_field}</div>
      {rows}
    </div>
    """


def build_html_map(
    conduit_wgs84,
    junctions_wgs84,
    conduit_color_field: str,
    conduit_colors: dict[str, str],
    output_path: Path,
    title: str,
    junction_color_field: str | None = None,
    junction_colors: dict[str, str] | None = None,
) -> None:
    """Save an interactive, pannable/zoomable HTML map color-coded by the given field(s)."""
    if len(conduit_wgs84) or len(junctions_wgs84):
        source = conduit_wgs84 if len(conduit_wgs84) else junctions_wgs84
        bounds = source.total_bounds  # minx, miny, maxx, maxy
        center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
    else:
        center = [28.6, 77.2]  # Delhi, fallback only

    fmap = folium.Map(location=center, zoom_start=14, tiles="CartoDB positron")

    for _, row in conduit_wgs84.iterrows():
        category = row[conduit_color_field]
        color = conduit_colors.get(category, "#999999")
        coords = [(lat, lon) for lon, lat in row.geometry.coords]
        popup = f"{row['NAME']} — {category}"
        folium.PolyLine(
            coords, color=color, weight=3 if category != "OK" else 1.5,
            opacity=0.9 if category != "OK" else 0.6, popup=popup,
        ).add_to(fmap)

    if len(junctions_wgs84):
        j_field = junction_color_field or conduit_color_field
        j_colors = junction_colors or conduit_colors
        for _, row in junctions_wgs84.iterrows():
            category = row.get(j_field, "OK")
            color = j_colors.get(category, "#999999")
            folium.CircleMarker(
                location=(row.geometry.y, row.geometry.x),
                radius=5 if category != "OK" else 2,
                color=color, fill=True, fill_color=color, fill_opacity=0.9,
                popup=f"{row['NAME']} — {category}",
            ).add_to(fmap)

    fmap.get_root().html.add_child(folium.Element(_category_html(conduit_color_field, conduit_colors)))
    title_html = f'<h3 style="position:fixed;top:12px;left:60px;z-index:9999;font-family:Segoe UI,Arial,sans-serif;">{title}</h3>'
    fmap.get_root().html.add_child(folium.Element(title_html))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(output_path))


def write_qml_lines(output_path: Path, field: str, colors: dict[str, str]) -> None:
    """Write a QGIS rule-based line style .qml so the shapefile color-codes itself when opened in QGIS."""
    _write_qml(output_path, field, colors, geometry_type="Line")


def write_qml_points(output_path: Path, field: str, colors: dict[str, str]) -> None:
    """Write a QGIS rule-based point style .qml so the shapefile color-codes itself when opened in QGIS."""
    _write_qml(output_path, field, colors, geometry_type="Point")


def _write_qml(output_path: Path, field: str, colors: dict[str, str], geometry_type: str) -> None:
    """
    Minimal but valid QGIS rule-based renderer .qml. Kept intentionally
    simple (flat categories, no priority ordering inside QGIS itself -
    the FLAW field written by the pipeline already carries the final,
    single category per feature) so it's easy to read and regenerate.
    """
    symbol_kind = "line" if geometry_type == "Line" else "marker"
    rules_xml = []
    symbols_xml = []
    for i, (label, color) in enumerate(colors.items()):
        expr = "ELSE" if label == "OK" else f'&quot;{field}&quot;=\'{label}\''
        else_attr = ' else="1"' if label == "OK" else ""
        rules_xml.append(f'<rule key="{{{i}}}" symbol="{i}" label="{label}" filter="{expr}"{else_attr}/>')
        if symbol_kind == "line":
            symbols_xml.append(
                f'<symbol type="line" name="{i}"><layer class="SimpleLine">'
                f'<Option type="Map"><Option type="QString" name="line_color" value="{color}"/>'
                f'<Option type="QString" name="line_width" value="{"0.8" if label != "OK" else "0.4"}"/></Option>'
                f'</layer></symbol>'
            )
        else:
            symbols_xml.append(
                f'<symbol type="marker" name="{i}"><layer class="SimpleMarker">'
                f'<Option type="Map"><Option type="QString" name="color" value="{color}"/>'
                f'<Option type="QString" name="size" value="{"3.2" if label != "OK" else "1.6"}"/></Option>'
                f'</layer></symbol>'
            )

    qml = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28">
  <renderer-v2 type="RuleRenderer" symbollevels="0">
    <rules key="{{root}}">
      {''.join(rules_xml)}
    </rules>
    <symbols>
      {''.join(symbols_xml)}
    </symbols>
  </renderer-v2>
</qgis>
"""
    output_path.write_text(qml, encoding="utf-8")
