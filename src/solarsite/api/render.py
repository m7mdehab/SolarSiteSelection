"""Report renderer interface for P3.1 — pluggable PDF generation.

P3.3 will replace this stub with a WeasyPrint implementation.

Interface contract
------------------
``render_report(job_id, job_dir) -> bytes``

Parameters
~~~~~~~~~~
job_id : str
    The job identifier (for logging / report title).
job_dir : pathlib.Path
    The job's on-disk directory.  It contains:
      - ``sites.geojson``  — candidate sites (WGS-84 GeoJSON)
      - ``lsi.nc``         — continuous LSI DataArray
      - ``class_raster.nc``— 5-class LSI DataArray
      - ``*.png``          — colormapped layer images
      - ``*.bounds.json``  — WGS-84 extent of each layer

Returns
~~~~~~~
bytes
    Raw PDF bytes ready to stream as ``application/pdf``.

Raises
~~~~~~
NotImplementedError
    The stub raises this; the API route catches it and returns 501.

How P3.3 slots in
-----------------
Replace (or monkey-patch) the ``render_report`` name in this module::

    # solarsite/api/render.py  (P3.3 version)
    from weasyprint import HTML, CSS

    def render_report(job_id: str, job_dir: Path) -> bytes:
        html = _build_html(job_id, job_dir)
        return HTML(string=html).write_pdf()

No other file needs changing.
"""

from __future__ import annotations

from pathlib import Path


def render_report(job_id: str, job_dir: Path) -> bytes:
    """Stub renderer — returns 501 until P3.3 wires in WeasyPrint.

    Parameters
    ----------
    job_id:
        Job identifier.
    job_dir:
        Path to the on-disk job directory (sites.geojson, layer PNGs, etc.).

    Returns
    -------
    bytes
        PDF bytes (not yet implemented).

    Raises
    ------
    NotImplementedError
        Always raised by this stub.
    """
    raise NotImplementedError(
        "PDF report rendering is not yet implemented. "
        "P3.3 will replace this stub with a WeasyPrint renderer. "
        "See solarsite/api/render.py for the interface contract."
    )
