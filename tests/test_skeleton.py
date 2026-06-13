"""Skeleton test: the package imports and core geospatial deps are importable."""

import solarsite


def test_package_imports() -> None:
    assert solarsite.__version__


def test_geospatial_stack_importable() -> None:
    import geopandas  # noqa: F401
    import pvlib  # noqa: F401
    import rasterio  # noqa: F401
    import rioxarray  # noqa: F401
    import xarray  # noqa: F401
