"""PDF report renderer for P3.3 — WeasyPrint implementation.

Public functions
----------------
build_report_html(job_id, job_dir) -> str
    Assemble the full report HTML string without importing WeasyPrint.
    Fully testable on Windows (no native GTK/Pango libs required).

render_report(job_id, job_dir) -> bytes
    Render the HTML to PDF bytes via WeasyPrint and return them.
    Raises ImportError (propagated) on hosts where WeasyPrint's native
    libs are absent; the route in app.py maps NotImplementedError → 501.

The route in app.py calls render_report() and streams the result as
``application/pdf``.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Data-source citation table
# ---------------------------------------------------------------------------

_TODAY: str = date.today().isoformat()

# Each entry: (name, url, retrieval_date)
_DATA_SOURCES: list[tuple[str, str, str]] = [
    (
        "PVGIS (EU Joint Research Centre) — Solar Radiation",
        "https://re.jrc.ec.europa.eu/pvg_tools/en/",
        _TODAY,
    ),
    (
        "Copernicus GLO-30 — Digital Elevation Model",
        "https://dataspace.copernicus.eu/explore-data/data-collections/"
        "copernicus-contributing-missions/cop-dem",
        _TODAY,
    ),
    (
        "OpenStreetMap / Overpass API — Roads, Railways, Power Lines, Urban Areas",
        "https://overpass-api.de/",
        _TODAY,
    ),
    (
        "ESA WorldCover v2 — Land Use / Land Cover",
        "https://esa-worldcover.org/en",
        _TODAY,
    ),
    (
        "Open-Meteo — Climate Variables (Temperature, Humidity, Wind Speed)",
        "https://open-meteo.com/",
        _TODAY,
    ),
    (
        "WDPA (World Database on Protected Areas)",
        "https://www.protectedplanet.net/en/thematic-areas/wdpa",
        _TODAY,
    ),
]

# ---------------------------------------------------------------------------
# Assumptions text
# ---------------------------------------------------------------------------

_ASSUMPTIONS_TEXT = (
    "<ul>"
    "<li><strong>Panel technology:</strong> Crystalline silicon (mono-Si), standard test condition"
    " efficiency ~20&nbsp;%, temperature coefficient ~&minus;0.4&nbsp;%/degC.</li>"
    "<li><strong>System losses:</strong> 14&nbsp;% total"
    " (wiring, inverter, soiling, shading).</li>"
    "<li><strong>Capacity factor estimation:</strong>"
    " Mean annual GHI x area x efficiency x (1&nbsp;&minus;&nbsp;losses)."
    " No detailed hourly TMY modelling unless energy data is present in"
    " <code>sites.geojson</code>.</li>"
    "<li><strong>LSI classification:</strong> Quantile-based 5-class scheme applied to the"
    " continuous LSI raster.  Classes 4 and 5 seed the candidate-site extraction.</li>"
    "<li><strong>Minimum site area:</strong> 0.5&nbsp;km2"
    " (approximate minimum viable utility-scale PV footprint).</li>"
    "<li><strong>AHP consistency threshold:</strong> CR&nbsp;&le;&nbsp;0.10 (Saaty 1980).</li>"
    "<li><strong>Hard-exclusion buffers:</strong> PTL safety buffer 4.8&nbsp;km;"
    " road/railway safety buffer 150&nbsp;m; urban area buffer 1.5&nbsp;km"
    " (after Habib et al. 2020).</li>"
    "<li><strong>Coordinate reference system:</strong>"
    " Analysis performed in a local UTM projection;"
    " outputs re-projected to WGS-84 (EPSG:4326) for display.</li>"
    "</ul>"
)

# ---------------------------------------------------------------------------
# CSS stylesheet (embedded)
# ---------------------------------------------------------------------------

_CSS = """
/* SolarSiteSelection PDF report stylesheet */
@page {
  size: A4;
  margin: 20mm 18mm 22mm 18mm;
  @bottom-center {
    content: "Page " counter(page) " of " counter(pages);
    font-size: 9pt;
    color: #666;
  }
}
body {
  font-family: "DejaVu Sans", Arial, sans-serif;
  font-size: 10pt;
  color: #222;
  line-height: 1.45;
}
h1 { font-size: 18pt; color: #1a5276; margin-bottom: 4pt; }
h2 { font-size: 13pt; color: #1a5276; border-bottom: 1px solid #aed6f1;
     padding-bottom: 2pt; margin-top: 16pt; }
h3 { font-size: 11pt; color: #2471a3; margin-top: 12pt; }
.meta { font-size: 9pt; color: #555; margin-bottom: 12pt; }
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 8.5pt;
  margin-top: 8pt;
}
th {
  background-color: #1a5276;
  color: #fff;
  padding: 4pt 6pt;
  text-align: left;
}
td {
  padding: 3pt 6pt;
  border-bottom: 1px solid #d5d8dc;
  vertical-align: top;
}
tr:nth-child(even) td { background-color: #eaf4fb; }
.img-block {
  text-align: center;
  margin: 10pt 0;
  page-break-inside: avoid;
}
.img-block img {
  max-width: 90%;
  max-height: 200pt;
  border: 1px solid #ccc;
}
.img-caption {
  font-size: 8pt;
  color: #555;
  margin-top: 3pt;
}
.weight-table td:last-child { text-align: right; font-weight: bold; }
.assumptions ul { padding-left: 18pt; }
.assumptions li { margin-bottom: 4pt; }
.citations ol { padding-left: 18pt; }
.citations li { margin-bottom: 4pt; }
.citations a { color: #1a5276; word-break: break-all; }
.section-break { page-break-before: always; }
.tag-public { color: #1a7a3c; font-size: 8pt; }
""".strip()

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for untrusted strings."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _b64_png(path: Path) -> str:
    """Return a data-URI for a PNG file, or empty string if the file is absent."""
    if not path.exists():
        return ""
    raw = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _fmt_float(value: object, decimals: int = 3) -> str:
    """Format a float, returning 'N/A' for NaN / None."""
    if value is None:
        return "N/A"
    try:
        f = float(value)  # type: ignore[arg-type]
        if math.isnan(f):
            return "N/A"
        return f"{f:.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_header(job_id: str, job_dir: Path) -> str:
    """Return the report header HTML."""
    # Try to read bounds for a brief AOI summary
    bounds_path = job_dir / "lsi.bounds.json"
    aoi_summary = ""
    if bounds_path.exists():
        try:
            bounds = json.loads(bounds_path.read_text(encoding="utf-8"))
            w = _fmt_float(bounds.get("west"), 4)
            s = _fmt_float(bounds.get("south"), 4)
            e = _fmt_float(bounds.get("east"), 4)
            n = _fmt_float(bounds.get("north"), 4)
            aoi_summary = (
                f"<p class='meta'>"
                f"<strong>AOI bounds (WGS-84):</strong> "
                f"W&nbsp;{w}° &nbsp;S&nbsp;{s}° &nbsp;E&nbsp;{e}° &nbsp;N&nbsp;{n}°"
                f"</p>"
            )
        except Exception:
            pass

    return f"""
<h1>SolarSiteSelection — PV Site Analysis Report</h1>
<p class='meta'>
  <strong>Job ID:</strong> {_html_escape(job_id)}&emsp;
  <strong>Generated:</strong> {_html_escape(_TODAY)}
</p>
{aoi_summary}
""".strip()


def _build_lsi_map(job_dir: Path) -> str:
    """Return the LSI map section with embedded image."""
    uri = _b64_png(job_dir / "lsi.png")
    if not uri:
        return ""
    caption = (
        "Figure 1 - Continuous LSI raster (0 = least suitable, 1 = most suitable)."
        " Colour scale: red to yellow to green."
    )
    return (
        "<h2>Land Suitability Index (LSI) Map</h2>"
        "<div class='img-block'>"
        f"  <img src='{uri}' alt='LSI map' />"
        f"  <p class='img-caption'>{caption}</p>"
        "</div>"
    )


def _build_methodology(job_dir: Path) -> str:
    """Return the methodology / weights section derived from the criteria registry."""
    from solarsite.analysis import load_registry

    try:
        registry = load_registry()
    except Exception as exc:
        err_msg = _html_escape(str(exc))
        return f"<h2>Methodology</h2><p>Could not load criteria registry: {err_msg}</p>"

    # --- AHP consistency ratio if present in a metadata file ---------------
    cr_html = ""
    meta_path = job_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cr_val = meta.get("ahp_cr")
            if cr_val is not None:
                consistent = float(cr_val) <= 0.10
                verdict = "&#10003; consistent" if consistent else "&#10007; inconsistent"
                cr_html = (
                    f"<p><strong>AHP Consistency Ratio (CR):</strong> "
                    f"{_fmt_float(cr_val, 4)} ({verdict})</p>"
                )
        except Exception:
            pass

    # --- Group + criteria weight table ------------------------------------
    rows = []
    for gkey, group in registry.groups.items():
        rows.append(
            f"<tr style='background:#d4e6f1'>"
            f"<td colspan='3'><strong>{_html_escape(group.name)}</strong> "
            f"<span style='color:#555;font-size:8pt'>(group key: {_html_escape(gkey)})</span>"
            f"</td>"
            f"<td style='text-align:right'><strong>{group.weight:.2f}</strong></td>"
            f"</tr>"
        )
        for criterion in group.criteria:
            if criterion.kind != "factor":
                continue
            global_w = registry.global_weight(criterion.key)
            rows.append(
                f"<tr>"
                f"<td>&nbsp;&nbsp;{_html_escape(criterion.name)}</td>"
                f"<td>{_html_escape(criterion.unit or '—')}</td>"
                f"<td style='text-align:right'>{criterion.local_weight:.3f}</td>"
                f"<td style='text-align:right'>{global_w:.4f}</td>"
                f"</tr>"
            )

    table_html = (
        "<table class='weight-table'>"
        "<thead><tr>"
        "<th>Criterion</th>"
        "<th>Unit</th>"
        "<th>Local weight</th>"
        "<th>Global weight</th>"
        "</tr></thead>"
        "<tbody>" + "\n".join(rows) + "</tbody>"
        "</table>"
    )

    # --- Hard-exclusion rules summary ------------------------------------
    excl_rows = []
    for rule in registry.hard_exclusion_rules:
        excl_rows.append(
            f"<tr><td>{_html_escape(rule.name)}</td><td>{_html_escape(rule.exclude_when)}</td></tr>"
        )
    excl_table = ""
    if excl_rows:
        excl_table = (
            "<h3>Hard-Exclusion Constraints</h3>"
            "<table>"
            "<thead><tr><th>Rule</th><th>Applied when</th></tr></thead>"
            "<tbody>" + "\n".join(excl_rows) + "</tbody>"
            "</table>"
        )

    return f"""
<h2>Methodology</h2>
<p>
  Multi-criteria suitability analysis using the Analytic Hierarchy Process (AHP)
  weighted overlay method (Saaty 1980; Habib et al. 2020).
  Criteria are grouped into three main categories with pre-defined group weights;
  local weights within each group sum to&nbsp;1.0.
</p>
{cr_html}
{table_html}
{excl_table}
""".strip()


def _build_criterion_thumbnails(job_dir: Path) -> str:
    """Return per-criterion PNG thumbnails for layers present in job_dir."""
    # All PNGs in job_dir that are NOT the main summary maps
    summary_names = {"lsi", "class_raster", "exclusion_mask"}
    items = []
    for png_path in sorted(job_dir.glob("*.png")):
        name = png_path.stem
        if name in summary_names:
            continue
        uri = _b64_png(png_path)
        if not uri:
            continue
        div_style = "display:inline-block;width:45%;margin:4pt;vertical-align:top"
        items.append(
            f"<div class='img-block' style='{div_style}'>"
            f"<img src='{uri}' alt='{_html_escape(name)}' />"
            f"<p class='img-caption'>{_html_escape(name)}</p>"
            f"</div>"
        )

    # Also include the standard summary maps (exclusion_mask, class_raster) if present
    for name in ("exclusion_mask", "class_raster"):
        png_path = job_dir / f"{name}.png"
        uri = _b64_png(png_path)
        if not uri:
            continue
        div_style = "display:inline-block;width:45%;margin:4pt;vertical-align:top"
        items.append(
            f"<div class='img-block' style='{div_style}'>"
            f"<img src='{uri}' alt='{_html_escape(name)}' />"
            f"<p class='img-caption'>{_html_escape(name)}</p>"
            f"</div>"
        )

    if not items:
        return ""

    return (
        "<h2>Per-Criterion Layer Thumbnails</h2>"
        "<div style='text-align:center'>" + "".join(items) + "</div>"
    )


def _build_site_table(job_dir: Path) -> str:
    """Return the candidate-site table from sites.geojson."""
    geojson_path = job_dir / "sites.geojson"
    if not geojson_path.exists():
        return "<h2>Candidate Sites</h2><p>No sites.geojson found.</p>"

    try:
        fc = json.loads(geojson_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (
            f"<h2>Candidate Sites</h2>"
            f"<p>Could not parse sites.geojson: {_html_escape(str(exc))}</p>"
        )

    features = fc.get("features", [])
    if not features:
        return "<h2>Candidate Sites</h2><p>No candidate sites were identified in this AOI.</p>"

    # Determine which optional columns are present across all features
    all_props: set[str] = set()
    for feat in features:
        all_props.update(feat.get("properties", {}).keys())

    # Core columns always shown; optional energy/economics columns appended if available
    optional_cols = [
        ("energy_kwh_year", "Energy (kWh/yr)"),
        ("lcoe_usd_kwh", "LCOE (USD/kWh)"),
        ("capacity_mw", "Capacity (MW)"),
        ("mean_ptl_dist", "Dist. PTL (m)"),
    ]
    extra_cols: list[tuple[str, str]] = [(k, label) for k, label in optional_cols if k in all_props]

    header_cells = (
        "<th>Rank</th>"
        "<th>Area (km²)</th>"
        "<th>Mean LSI</th>"
        "<th>Max LSI</th>"
        "<th>Centroid Lon</th>"
        "<th>Centroid Lat</th>"
        + "".join(f"<th>{_html_escape(label)}</th>" for _, label in extra_cols)
    )

    rows = []
    for feat in features:
        props = feat.get("properties", {})
        cells = (
            f"<td>{_html_escape(str(props.get('rank', 'N/A')))}</td>"
            f"<td>{_fmt_float(props.get('area_km2'))}</td>"
            f"<td>{_fmt_float(props.get('mean_lsi'))}</td>"
            f"<td>{_fmt_float(props.get('max_lsi'))}</td>"
            f"<td>{_fmt_float(props.get('centroid_lon'), 4)}</td>"
            f"<td>{_fmt_float(props.get('centroid_lat'), 4)}</td>"
            + "".join(f"<td>{_fmt_float(props.get(k))}</td>" for k, _ in extra_cols)
        )
        rows.append(f"<tr>{cells}</tr>")

    return (
        f"<h2>Candidate Sites ({len(features)} identified)</h2>"
        "<table>"
        f"<thead><tr>{header_cells}</tr></thead>"
        "<tbody>" + "\n".join(rows) + "</tbody>"
        "</table>"
    )


def _build_assumptions() -> str:
    """Return the assumptions appendix section."""
    return f"""
<h2 class='section-break'>Appendix A — Assumptions &amp; Limitations</h2>
<div class='assumptions'>
{_ASSUMPTIONS_TEXT}
<p style='margin-top:8pt;font-size:8.5pt;color:#555'>
  Results are indicative only. Site-specific ground-truthing, grid-connection
  studies, and regulatory review are required before any investment decision.
</p>
</div>
""".strip()


def _build_citations() -> str:
    """Return the data-source citations section."""
    items = []
    for name, url, retrieval in _DATA_SOURCES:
        items.append(
            f"<li>"
            f"<strong>{_html_escape(name)}</strong><br/>"
            f"URL: <a href='{_html_escape(url)}'>{_html_escape(url)}</a><br/>"
            f"Retrieval date: {_html_escape(retrieval)}"
            f"</li>"
        )
    return (
        "<h2>Data Sources &amp; Citations</h2>"
        "<div class='citations'>"
        "<ol>" + "\n".join(items) + "</ol>"
        "<p style='font-size:8.5pt;color:#555;margin-top:8pt'>"
        "Habib&nbsp;et&nbsp;al.&nbsp;(2020). GIS-based multi-criteria analysis for PV siting. "
        "<em>Renewable &amp; Sustainable Energy Reviews</em>.</p>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Main public functions
# ---------------------------------------------------------------------------


def build_report_html(job_id: str, job_dir: Path) -> str:
    """Assemble the full report HTML string — no WeasyPrint dependency.

    Parameters
    ----------
    job_id:
        Job identifier (used in the report title).
    job_dir:
        Path to the on-disk job directory containing sites.geojson, *.png,
        *.bounds.json, and optionally metadata.json.

    Returns
    -------
    str
        A complete HTML document ready for WeasyPrint or browser inspection.
    """
    sections = [
        _build_header(job_id, job_dir),
        _build_lsi_map(job_dir),
        _build_methodology(job_dir),
        _build_criterion_thumbnails(job_dir),
        _build_site_table(job_dir),
        _build_assumptions(),
        _build_citations(),
    ]

    body = "\n\n".join(s for s in sections if s)

    return (
        "<!DOCTYPE html>\n"
        "<html lang='en'>\n"
        "<head>\n"
        "  <meta charset='utf-8'/>\n"
        f"  <title>SolarSiteSelection Report — {_html_escape(job_id)}</title>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>"
    )


def render_report(job_id: str, job_dir: Path) -> bytes:
    """Render the analysis report to PDF bytes via WeasyPrint.

    Parameters
    ----------
    job_id:
        Job identifier.
    job_dir:
        Path to the on-disk job directory (sites.geojson, layer PNGs, etc.).

    Returns
    -------
    bytes
        Raw PDF bytes ready to stream as ``application/pdf``.

    Raises
    ------
    NotImplementedError
        Raised (wrapping the underlying ImportError or OSError) when
        WeasyPrint's native GTK/Pango/Cairo libraries are absent (e.g. on
        Windows without the runtime installed).  The route in app.py maps
        this to HTTP 501 Not Implemented.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:
        raise NotImplementedError(
            "PDF rendering requires WeasyPrint with native GTK/Pango/Cairo libraries. "
            "Install the WeasyPrint runtime (see https://doc.courtbouillon.org/weasyprint/"
            "stable/first_steps.html#installation) or run in the Docker/CI environment "
            f"where libs are pre-installed. Underlying error: {exc}"
        ) from exc

    html_string = build_report_html(job_id, job_dir)
    return HTML(string=html_string).write_pdf()  # type: ignore[no-any-return]
