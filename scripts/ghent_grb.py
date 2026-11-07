"""Building colours from the 3D GRB LOD1 KML tile set in Lambert 72.

The ``GRBGebL1D1_KML_LB72.zip`` download is the 1 km KML edition of the 3D GRB
block model: one Placemark per GRB building or building part, each pointing at a
``styleUrl`` whose ``PolyStyle`` carries the official legend colour.  The colours
encode the reference height (``HN_P99``), not facade paint:

    white = 0 m, lightsalmon = 0-5 m, tomato = 5-10 m,
    firebrick = 10-25 m, indigo = >25 m.

The colours are aabbggrr (KML order), so ``647aa0ff`` is lightsalmon
(255, 160, 122).  We rasterize the coloured footprints on the same 1 m grid as
the voxel map and let the builder take each building's colour from them.  The
KML is in WGS84; tiles are named by their Lambert 72 kilometre
(``…_105_194`` covers easting 105-106 km, northing 194-195 km).
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.geometry import Polygon

import ghent_area

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw"
CACHE = ghent_area.GRB_CACHE
DEFAULT_ZIP = RAW / "GRBGebL1D1_KML_LB72.zip"
from ghent_area import D, EAST, NORTH, SOUTH, W, WEST

STYLE_RE = re.compile(
    r'<kml:Style id="([^"]+)">.*?<kml:PolyStyle>\s*<kml:color>([0-9a-fA-F]{8})</kml:color>',
    re.S,
)
STYLEMAP_RE = re.compile(r'<kml:StyleMap id="([^"]+)">(.*?)</kml:StyleMap>', re.S)
PLACEMARK_RE = re.compile(r"<kml:Placemark>(.*?)</kml:Placemark>", re.S)
COORD_RE = re.compile(r"<kml:coordinates>(.*?)</kml:coordinates>", re.S)
STYLEURL_RE = re.compile(r"<kml:styleUrl>#([^<]+)</kml:styleUrl>")
DATA_RE = re.compile(r'<kml:SimpleData name="([^"]+)">([^<]*)</kml:SimpleData>')


def kml_colour(value: str) -> tuple[int, int, int]:
    """Convert KML ``aabbggrr`` to ``(r, g, b)``."""
    if len(value) != 8:
        raise ValueError(value)
    return int(value[6:8], 16), int(value[4:6], 16), int(value[2:4], 16)


def parse_styles(text: str) -> dict[str, tuple[int, int, int]]:
    """Return ``{stylemap_or_style_name: rgb}`` for a KML document."""
    colours = {name: kml_colour(value) for name, value in STYLE_RE.findall(text)}
    result = dict(colours)
    for name, body in STYLEMAP_RE.findall(text):
        normal = re.search(
            r"<(?:kml:)?key>normal</(?:kml:)?key>\s*<(?:kml:)?styleUrl>#([^<]+)</(?:kml:)?styleUrl>", body
        )
        if normal and normal.group(1) in colours:
            result[name] = colours[normal.group(1)]
    return result


def tile_names(zip_path: Path) -> list[str]:
    """KML entries for the 1 km tiles touching the map, including a 1 km margin."""
    x0, x1 = WEST // 1000 - 1, (EAST - 1) // 1000 + 1
    y0, y1 = SOUTH // 1000 - 1, (NORTH - 1) // 1000 + 1
    wanted = {f"GRBGebL1D1_{x}_{y}.kml" for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
    with zipfile.ZipFile(zip_path) as archive:
        return sorted(name for name in archive.namelist() if name.split("\\")[-1] in wanted)


def parse_placemarks(text: str, styles: dict[str, tuple[int, int, int]],
                     project: Transformer) -> list[tuple[tuple[int, int, int], str, str, float, Polygon]]:
    """Coloured footprints in grid metres ``(x, y)``, with entity, label and area."""
    out = []
    for body in PLACEMARK_RE.findall(text):
        match = STYLEURL_RE.search(body)
        coords = COORD_RE.search(body)
        if match is None or coords is None or match.group(1) not in styles:
            continue
        data = dict(DATA_RE.findall(body))
        points = []
        for token in coords.group(1).split():
            lon, lat, _alt = (float(value) for value in token.split(","))
            east, north = project.transform(lon, lat)
            points.append((east - WEST, NORTH - north))
        if len(points) < 4:
            continue
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            continue
        out.append((styles[match.group(1)], data.get("ENTITEIT", ""), data.get("LBLTYPE", ""),
                    polygon.area, polygon))
    return out


def load(zip_path: Path = DEFAULT_ZIP, cache: Path = CACHE, refresh: bool = False) -> dict:
    """Return ``colour`` (D, W, 3) uint8, ``mask`` (D, W) bool and ``footprint`` (D, W) int32 ids for the map."""
    if not zip_path.exists():
        raise FileNotFoundError(f"Missing {zip_path}; download the 3D GRB LOD1 KML tile set")
    if not refresh and cache.exists() and cache.stat().st_mtime >= zip_path.stat().st_mtime:
        data = np.load(cache)
        if "footprint" in data:
            return {"colour": data["colour"], "mask": data["mask"], "footprint": data["footprint"],
                    "colours": data["colours"].tolist(), "stats": json.loads(str(data["stats"]))}

    project = Transformer.from_crs(4326, 31370, always_xy=True)
    features = []
    with zipfile.ZipFile(zip_path) as archive:
        for name in tile_names(zip_path):
            text = archive.read(name).decode("utf-8-sig")
            styles = parse_styles(text)
            features.extend(parse_placemarks(text, styles, project))
    if not features:
        raise RuntimeError(f"No KML building footprints found in {zip_path}")

    # Larger footprints first, and whole buildings before their parts, so a
    # chimney or a floor part takes the cell it really covers.
    order = {"Gbg": 0, "Knw": 1, "Gba": 2}
    features.sort(key=lambda item: (order.get(item[1], 3), -item[3]))
    colours, index = [], {}
    shapes = []
    for rgb, entity, label, area, polygon in features:
        if rgb not in index:
            index[rgb] = len(colours) + 1
            colours.append(rgb)
        shapes.append((polygon, index[rgb]))
    ids = rasterize(shapes, out_shape=(D, W), transform=Affine.identity(), fill=0,
                    dtype=np.uint16, all_touched=False)
    table = np.zeros((len(colours) + 1, 3), np.uint8)
    table[1:] = np.array(colours, np.uint8)
    colour = table[ids]
    mask = ids > 0
    # Each footprint on its own (1..n): one house, one facade and one roof, however tightly a block is built.
    footprint = rasterize(((polygon, i) for i, (*_rest, polygon) in enumerate(features, 1)), out_shape=(D, W),
                          transform=Affine.identity(), fill=0, dtype=np.int32, all_touched=False)

    entity_area: dict[str, float] = {}
    label_area: dict[str, float] = {}
    for _rgb, entity, label, area, _polygon in features:
        entity_area[entity] = entity_area.get(entity, 0.0) + area
        label_area[label] = label_area.get(label, 0.0) + area
    stats = {
        "placemarks": len(features),
        "colours": {f"#{r:02x}{g:02x}{b:02x}": int(sum(1 for f in features if f[0] == (r, g, b)))
                    for r, g, b in colours},
        "area_by_entity": {k: round(v) for k, v in sorted(entity_area.items(), key=lambda kv: -kv[1])},
        "area_by_label": {k: round(v) for k, v in sorted(label_area.items(), key=lambda kv: -kv[1])[:8]},
        "cells": int(mask.sum()),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, colour=colour, mask=mask, footprint=footprint,
                        colours=np.array(colours, np.uint8), stats=json.dumps(stats))
    return {"colour": colour, "mask": mask, "footprint": footprint, "colours": colours, "stats": stats}


if __name__ == "__main__":
    result = load(refresh=True)
    print(result["stats"])
