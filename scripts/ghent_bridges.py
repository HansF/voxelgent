"""Bridges from OpenStreetMap, sized and placed with the DHMV II surface model.

The city's terrain dips into the canal under every bridge, so the map had no decks (and where the building tiles
carried a bridge it was a solid block down to the water).  Here each OSM bridge -- the ``bridge=*`` ways (road, tram,
rail, path) and the ``man_made=bridge`` outlines -- becomes a floating slab:

* footprint: the outline of a man_made=bridge, else the line buffered by half its width (the ``width`` tag, or a
  width by road class), flat ends; the DSM then widens it to the deck it really measures (cells within 0.9 m of the
  deck profile and well above the ground) and the nominal core keeps it connected;
* deck height: the median DSM along the bridge (1 m bins, smoothed); where the 2013-15 LiDAR has no deck, a straight
  line between the ground at both ends.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from scipy import ndimage
from shapely.geometry import LineString, Polygon

WIDTHS = {"motorway": 14, "trunk": 12, "primary": 11, "secondary": 9, "tertiary": 8, "unclassified": 6.5, "residential": 6.5,
          "living_street": 6, "service": 4, "pedestrian": 4, "track": 3, "cycleway": 3, "footway": 2.5, "path": 2.5,
          "steps": 2.5, "construction": 5, "platform": 3, "tram": 5, "rail": 7, "light_rail": 6, "subway": 5}
PROJECT = Transformer.from_crs(4326, 31370, always_xy=True)


def _width(tags: dict) -> float:
    try:
        return max(1.5, min(30.0, float(str(tags["width"]).split()[0].replace(",", "."))))
    except (KeyError, ValueError):
        return float(WIDTHS.get(tags.get("highway") or tags.get("railway") or "", 5))


def load(path: Path, west: int, north: int, width: int, depth: int) -> list[dict]:
    """Bridges in grid metres: dicts with ``axis`` (LineString), ``outline`` (Polygon or None), ``half`` width."""
    if not path.exists():
        return []
    out = []
    for item in json.loads(path.read_text())["elements"]:
        tags = item.get("tags", {})
        if item["type"] != "way" or len(item.get("geometry", [])) < 2:
            continue
        if tags.get("tunnel") == "yes" or tags.get("highway") == "construction" and tags.get("construction") == "no":
            continue
        e, n = PROJECT.transform([p["lon"] for p in item["geometry"]], [p["lat"] for p in item["geometry"]])
        pts = list(zip(np.asarray(e) - west, north - np.asarray(n)))
        area = tags.get("man_made") == "bridge" and len(pts) >= 4 and pts[0] == pts[-1]
        if area:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area < 6:
                continue
            rect = np.array(poly.minimum_rotated_rectangle.exterior.coords)[:4]
            sides = [(np.hypot(*(rect[(i + 1) % 4] - rect[i])), i) for i in range(4)]
            _, i = max(sides)  # the long side: the axis runs between the midpoints of the two short sides
            a, b, c, d = rect[i], rect[(i + 1) % 4], rect[(i + 2) % 4], rect[(i + 3) % 4]
            axis = LineString([(a + d) / 2, (b + c) / 2])
            out.append(dict(axis=axis, outline=poly, half=poly.area / max(axis.length, 1) / 2, name=tags.get("name", "")))
        elif tags.get("bridge") not in (None, "no") and tags.get("highway") != "steps":
            line = LineString(pts)
            if line.length < 3 or not line.intersects(Polygon([(0, 0), (width, 0), (width, depth), (0, depth)])):
                continue
            out.append(dict(axis=line, outline=None, half=_width(tags) / 2, name=tags.get("name", "")))
    return out


def _mask(geom, x0, y0, w, h):
    return rasterize([(geom, 1)], out_shape=(h, w), transform=Affine.translation(x0, y0), fill=0, dtype=np.uint8).astype(bool)


def build(bridges: list[dict], dsm: np.ndarray, ground: np.ndarray):
    """-> ``(deck, zone)``: deck height in metres (NaN where none) and the cells of every bridge span
    (where whatever else the map has -- a solid block from the building tiles -- must go)."""
    D, W = dsm.shape
    deck = np.full((D, W), np.nan, np.float32)
    zone = np.zeros((D, W), bool)
    for br in sorted(bridges, key=lambda b: b["outline"] is None):  # outlines first, lines refine over them
        axis, half = br["axis"], br["half"]
        nominal = br["outline"] if br["outline"] is not None else axis.buffer(half, cap_style=2)
        wide = nominal.buffer(2.5, cap_style=2, join_style=2)
        bx0, by0, bx1, by1 = (int(np.floor(v)) for v in wide.bounds)
        x0, y0, x1, y1 = max(0, bx0), max(0, by0), min(W, bx1 + 1), min(D, by1 + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        w, h = x1 - x0, y1 - y0
        nom, wid = _mask(nominal, x0, y0, w, h), _mask(wide, x0, y0, w, h)
        core = _mask(axis.buffer(max(1.0, half * 0.55), cap_style=2), x0, y0, w, h)
        yy, xx = np.nonzero(wid)
        s = shapely.line_locate_point(axis, shapely.points(xx + x0 + 0.5, yy + y0 + 0.5))
        length = max(axis.length, 1.0)
        nb = int(np.ceil(length)) + 1
        bins = np.clip(s.astype(int), 0, nb - 1)
        sub_dsm, sub_gr = dsm[y0:y1, x0:x1], ground[y0:y1, x0:x1]
        in_nom = nom[yy, xx]
        prof = np.full(nb, np.nan)
        for k in np.unique(bins[in_nom]):
            prof[k] = np.median(sub_dsm[yy, xx][in_nom & (bins == k)])
        if np.isfinite(prof).sum() >= 2:
            idx = np.arange(nb)
            prof = np.interp(idx, idx[np.isfinite(prof)], prof[np.isfinite(prof)])
            prof = ndimage.median_filter(prof, size=5, mode="nearest")
        gnd_nom = sub_gr[yy, xx][in_nom]
        measured = np.isfinite(prof).all() and in_nom.any() and \
            float(np.median(prof[bins[in_nom]] - gnd_nom)) >= 1.0
        if not measured:
            # No deck in the LiDAR: a straight line between the ground at both ends, if that spans a gap.
            ends = []
            for frac in (0.0, 1.0):
                px, py = axis.interpolate(frac, normalized=True).coords[0]
                cx0, cy0 = int(px) - x0, int(py) - y0
                win = sub_gr[max(0, cy0 - 2):cy0 + 3, max(0, cx0 - 2):cx0 + 3]
                ends.append(float(np.nanmax(win)) if win.size else np.nan)
            if not np.isfinite(ends).all() or in_nom.sum() == 0 or float(np.median(np.mean(ends) - gnd_nom)) < 1.5:
                continue
            prof = np.linspace(ends[0], ends[1], nb)
        ph = prof[bins]
        near = np.abs(sub_dsm[yy, xx] - ph) <= 0.9
        above = (sub_dsm[yy, xx] - sub_gr[yy, xx]) >= 1.0
        cand = np.zeros((h, w), bool)
        cand[yy, xx] = (near & above & measured) | (core[yy, xx] & ((ph - sub_gr[yy, xx]) >= 1.0))
        cand |= nom & core
        height = np.full((h, w), np.nan, np.float32)
        height[yy, xx] = ph
        cand &= np.isfinite(height) & ((height - sub_gr) >= 1.0)
        cand = ndimage.binary_closing(cand, iterations=1)
        lab, n = ndimage.label(cand)
        if n:
            sizes = np.bincount(lab.ravel())
            keep = sizes >= 6
            keep[0] = False
            cand = keep[lab]
        if not cand.any():
            continue
        win_d = deck[y0:y1, x0:x1]
        win_d[cand] = height[cand]
        span = np.zeros((h, w), bool)
        span[yy, xx] = (ph - sub_gr[yy, xx]) >= 1.5
        zone[y0:y1, x0:x1] |= span & wid
    return deck, zone
