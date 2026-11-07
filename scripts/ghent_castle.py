"""Gravensteen: a detailed model dropped into the voxel map in place of the city's rough block.

Model: "Gravensteen" by Haplo1988, https://www.thingiverse.com/thing:4187726, CC BY (Creative Commons Attribution).
Binary STL in metres: about 73 x 77 m, 30 m tall, base at z = 6.5, 600k triangles, no georeference.

Placement was fitted by hand against the DHMV II surface model (footprint overlap): the model's own centre (the
bounding-box centre of everything above z = 9) sits at the Lambert 72 point below, rotated -1 degree.  The model
base is then lifted onto the local ground.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from scipy import ndimage

import ghent_area

MODEL = ghent_area.ROOT / "data/raw/gravensteen/files/Gravensteen.STL"
CENTRE_MODEL = (-10.74645615, -15.63262177)  # model x, y that lands on CENTRE_LAMBERT
CENTRE_LAMBERT = (104573.0, 194306.0)  # easting, northing (Lambert 72)
ROTATION_DEG = -1.0
BASE_Z = 6.5  # the model's ground floor
CREDIT = "Gravensteen model by Haplo1988 (Thingiverse, CC BY)"


def read_model(path: Path = MODEL) -> np.ndarray:
    raw = path.read_bytes()
    count = int.from_bytes(raw[80:84], "little")
    if len(raw) != 84 + count * 50:
        raise ValueError(f"Not a binary STL: {path}")
    dtype = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    return np.frombuffer(raw, dtype=dtype, count=count, offset=84)["vertices"].astype(np.float64)


def to_grid(triangles: np.ndarray, west: int, north: int) -> np.ndarray:
    """Model triangles -> map grid metres (x east of `west`, y south of `north`), z unchanged."""
    t = math.radians(ROTATION_DEG)
    c, s = math.cos(t), math.sin(t)
    x, y = triangles[:, :, 0] - CENTRE_MODEL[0], triangles[:, :, 1] - CENTRE_MODEL[1]
    east = CENTRE_LAMBERT[0] + x * c - y * s
    northing = CENTRE_LAMBERT[1] + x * s + y * c
    out = triangles.copy()
    out[:, :, 0], out[:, :, 1] = east - west, north - northing
    return out


def surface(width: int, depth: int, west: int, north: int):
    """Highest model surface per 1 m cell: ``(row0, col0, heights)`` with NaN where the model is absent, or None
    when the castle lies outside the map.  Heights are the model's own z; add the vertical offset yourself."""
    if not MODEL.exists():
        return None
    tri = to_grid(read_model(), west, north)
    x0, y0 = np.floor(tri[:, :, :2].reshape(-1, 2).min(0)).astype(int) - 1
    x1, y1 = np.ceil(tri[:, :, :2].reshape(-1, 2).max(0)).astype(int) + 2
    if x1 <= 0 or y1 <= 0 or x0 >= width or y0 >= depth:
        return None
    w, h = x1 - x0, y1 - y0
    top = np.full((h, w), -np.inf, np.float32)
    span = (tri[:, :, :2].max(1) - tri[:, :, :2].min(1)).max(1)
    small = span <= 1.0
    # Small triangles: sample the vertices, edge midpoints and centroids; the highest sample in a cell wins.
    t = tri[small]
    pts = np.concatenate([t.reshape(-1, 3), t.mean(1), (t[:, 0] + t[:, 1]) / 2, (t[:, 1] + t[:, 2]) / 2,
                          (t[:, 0] + t[:, 2]) / 2])
    ix, iy = np.floor(pts[:, 0]).astype(int) - x0, np.floor(pts[:, 1]).astype(int) - y0
    ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
    np.maximum.at(top, (iy[ok], ix[ok]), pts[ok, 2].astype(np.float32))
    # Large (flat floor, big roof) triangles: barycentric z at every cell centre they cover.
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tri[~small]:
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-8:
            continue
        a0, b0 = int(math.floor(min(ax, bx, cx))), int(math.floor(min(ay, by, cy)))
        a1, b1 = int(math.ceil(max(ax, bx, cx))), int(math.ceil(max(ay, by, cy)))
        a0, a1, b0, b1 = max(a0, x0), min(a1, x1), max(b0, y0), min(b1, y1)
        if a0 >= a1 or b0 >= b1:
            continue
        yy, xx = np.ogrid[b0:b1, a0:a1]
        xx, yy = xx + 0.5, yy + 0.5
        u = ((by - cy) * (xx - cx) + (cx - bx) * (yy - cy)) / den
        v = ((cy - ay) * (xx - cx) + (ax - cx) * (yy - cy)) / den
        inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1.000001)
        z = (u * az + v * bz + (1 - u - v) * cz).astype(np.float32)
        window = top[b0 - y0:b1 - y0, a0 - x0:a1 - x0]
        np.maximum(window, np.where(inside, z, -np.inf), out=window)
    top[~np.isfinite(top)] = np.nan
    # Close single-cell gaps between samples inside the castle without growing its outline.
    filled = ndimage.maximum_filter(np.nan_to_num(top, nan=-1e9), size=3)
    gap = ~np.isfinite(top) & ndimage.binary_closing(np.isfinite(top), iterations=1)
    top[gap] = filled[gap]
    return y0, x0, top


def place(width: int, depth: int, west: int, north: int, ground: np.ndarray):
    """The castle on a ``(depth, width)`` map: ``(zone, building, roof)`` or None.

    ``zone``     cells of the castle's outline, filled: whatever else the map has there is replaced;
    ``building`` cells above the ground (walls, towers, roofs), a subset of the zone;
    ``roof``     absolute heights in metres for those cells (NaN elsewhere).
    """
    placed = surface(width, depth, west, north)
    if placed is None:
        return None
    y0, x0, top = placed
    h, w = top.shape
    ry0, ry1, rx0, rx1 = max(0, y0), min(depth, y0 + h), max(0, x0), min(width, x0 + w)
    top = top[ry0 - y0:ry1 - y0, rx0 - x0:rx1 - x0]
    sub = (slice(ry0, ry1), slice(rx0, rx1))
    outline = ndimage.binary_fill_holes(ndimage.binary_closing(np.isfinite(top), iterations=2))
    floor = float(np.median(ground[sub][np.isfinite(top) & (top < BASE_Z + 0.5)])) if (np.isfinite(top) & (top < BASE_Z + 0.5)).any() \
        else float(np.median(ground[sub][outline]))
    lifted = top + (floor - BASE_Z)
    solid = np.isfinite(lifted) & (lifted > ground[sub] + 1.5)
    zone = np.zeros((depth, width), bool)
    building = np.zeros((depth, width), bool)
    roof = np.full((depth, width), np.nan, np.float32)
    zone[sub] = outline | solid
    building[sub] = solid
    roof[sub] = np.where(solid, lifted, np.nan)
    return zone, building, roof
