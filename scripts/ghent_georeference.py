"""Fit a local WGS84 -> voxel-grid transform against PROJ's Lambert 72 transform.

The quadratic is bounded to central Ghent and verified on a separate sample grid.
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
    lon, lat = np.meshgrid(np.linspace(3.70, 3.76, 9), np.linspace(51.035, 51.075, 9))
    east, northing = transform.transform(lon, lat)
    design = basis(lon, lat).reshape(-1, 6)
    x = np.linalg.lstsq(design, (east - west).ravel(), rcond=None)[0]
    y = np.linalg.lstsq(design, (north - northing).ravel(), rcond=None)[0]
    check_lon, check_lat = np.meshgrid(np.linspace(3.701, 3.759, 12), np.linspace(51.036, 51.074, 12))
    check_east, check_north = transform.transform(check_lon, check_lat)
    design = basis(check_lon, check_lat)
    error = np.hypot(design @ x - (check_east - west), design @ y - (north - check_north))
    if error.max() > 0.02:
        raise ValueError(f"Local projection error too large: {error.max()} metres")
    return dict(crs="EPSG:31370", origin=[west, north], W=width, D=depth,
                anchor=[3.725, 51.055], scale=100, x=x.tolist(), y=y.tolist(),
                bounds=[3.70, 51.035, 3.76, 51.075], max_error_m=float(error.max()))


if __name__ == "__main__":
    target = ROOT / "web/public/ghent/georeference.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(make_georeference(), separators=(",", ":")))
    print(target)
