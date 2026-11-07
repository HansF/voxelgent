"""Fit a local WGS84 -> voxel-grid transform against PROJ's Lambert 72 transform.

The quadratic is bounded to the map area (central Ghent, or the R4 ring) and verified on a separate sample grid.
This lets the Node transit proxy project positions without a second GIS runtime.
"""
import json
from pathlib import Path

import numpy as np
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]


def basis(lon, lat):
    u, v = (np.asarray(lon) - 3.725) * 100, (np.asarray(lat) - 51.055) * 100
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=-1)


def make_georeference(west=104000, north=195000, width=2000, depth=2000):
    transform = Transformer.from_crs(4326, 31370, always_xy=True)
    inverse = Transformer.from_crs(31370, 4326, always_xy=True)
    pad = 500
    cx, cy = inverse.transform([west - pad, west + width + pad, west - pad, west + width + pad],
                               [north - depth - pad, north - depth - pad, north + pad, north + pad])
    lon0, lon1, lat0, lat1 = min(cx), max(cx), min(cy), max(cy)
    lon, lat = np.meshgrid(np.linspace(lon0, lon1, 12), np.linspace(lat0, lat1, 12))
    east, northing = transform.transform(lon, lat)
    design = basis(lon, lat).reshape(-1, 6)
    x = np.linalg.lstsq(design, (east - west).ravel(), rcond=None)[0]
    y = np.linalg.lstsq(design, (north - northing).ravel(), rcond=None)[0]
    check_lon, check_lat = np.meshgrid(np.linspace(lon0 + 0.0003, lon1 - 0.0003, 15), np.linspace(lat0 + 0.0003, lat1 - 0.0003, 15))
    check_east, check_north = transform.transform(check_lon, check_lat)
    design = basis(check_lon, check_lat)
    error = np.hypot(design @ x - (check_east - west), design @ y - (north - check_north))
    if error.max() > 0.05:
        raise ValueError(f"Local projection error too large: {error.max()} metres")
    return dict(crs="EPSG:31370", origin=[west, north], W=width, D=depth,
                anchor=[3.725, 51.055], scale=100, x=x.tolist(), y=y.tolist(),
                bounds=[lon0, lat0, lon1, lat1], max_error_m=float(error.max()))


if __name__ == "__main__":
    import ghent_area
    target = ROOT / "web/public/ghent/georeference.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(make_georeference(ghent_area.WEST, ghent_area.NORTH, ghent_area.W, ghent_area.D), separators=(",", ":")))
    print(target)
