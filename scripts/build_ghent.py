"""Voxelize Ghent from the current STL roofs, official DHMV II terrain/canopy,
the 3D GRB LOD1 footprints and the summer orthophoto.

python scripts/fetch_ghent.py && python scripts/ghent_trees.py
python scripts/vlaanderen.py            # DHMV II DSM/DTM + summer ortho (cached)
python scripts/ghent_grb.py             # 3D GRB LOD1 KML footprints (cached)
python scripts/build_ghent.py

Geometry: Lambert 72, 1 m voxels.  Terrain is the STL; buildings are GRB
footprints with DHMV II roof heights (STL only for newer buildings).

* DHMV II DTM fills the 0.4 % of terrain the STL missed.
* DHMV II DSM minus DTM, masked by summer-ortho greenness and the tree
  inventory, gives every real crown its measured outline and height instead of
  a sphere.
* The 3D GRB KML is used only as a 2-D footprint mask (which X/Y is an official
  building); its flat LOD1 heights and legend colours are ignored.  Roof
  heights come from the DHMV II DSM, the STL fills in newer buildings.
* The 2021 summer orthophoto supplies ground, roof and facade colours.

Output is separate from Warsaw: web/public/ghent.
"""
import gzip
import json
import math
import os
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from scipy import ndimage
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union

import ghent_area
import ghent_bridges
import ghent_canopy
import ghent_castle
import ghent_grb
import tiles
import vlaanderen
from ghent_georeference import make_georeference

from ghent_area import D, NORTH, RAW, ROOT, W, WEST

OUT = ROOT / "web/public/ghent"
CHUNK, HALO = 1024, 16  # voxel volumes are built 1 km at a time (a whole level-8 tile node) plus a halo
AREA = box(0, 0, W, D)
PROJECT = Transformer.from_crs(4326, 31370, always_xy=True)
# Palette alpha stores the viewer's material id, not opacity.  These first
# entries stay stable: soil (1), trunks (6), leaves (7-9) and the wall colours.
PALETTE = [[0, 0, 0, 0], [158, 153, 140, 0], [103, 128, 125, 4],
           [92, 98, 100, 0], [185, 177, 159, 0], [104, 133, 78, 8],
           [101, 70, 48, 0], [66, 98, 50, 3], [88, 120, 58, 3], [50, 78, 44, 3],
           [160, 101, 73, 1], [179, 140, 104, 1], [203, 190, 160, 1],
           [144, 132, 113, 1], [185, 170, 145, 1]]
SOIL, TRUNK, LEAF, GRASS, WATER = 1, 6, 7, 8, 4
LEAF_IDS = (7, 8, 9)
BELFORT = (104849.0, 193918.0)  # Lambert 72


def note(*args):
    print(*args, flush=True)


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def gz(name, payload):
    with gzip.GzipFile(OUT / name, "wb", compresslevel=9, mtime=0) as file:
        file.write(payload)


def read_stl(path):
    raw = path.read_bytes()
    count = int.from_bytes(raw[80:84], "little")
    if len(raw) != 84 + count * 50:
        raise ValueError(f"Not a binary STL: {path}")
    dtype = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    triangles = np.frombuffer(raw, dtype=dtype, count=count, offset=84)["vertices"].astype(np.float64)
    triangles[:, :, 0] -= WEST
    triangles[:, :, 1] = NORTH - triangles[:, :, 1]
    return triangles


def _raster_file(path):
    """One STL tile -> (x0, y0, surface window) with the highest surface at each cell centre."""
    triangles = read_stl(path)
    note(path.name, len(triangles), "triangles")
    x0, y0 = np.maximum(np.floor(triangles[:, :, :2].reshape(-1, 2).min(0)).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(triangles[:, :, :2].reshape(-1, 2).max(0)).astype(int), [W, D])
    if x0 >= x1 or y0 >= y1:
        return None
    surface = np.full((y1 - y0, x1 - x0), np.nan, np.float32)
    for tri in triangles:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-8:  # Vertical wall: the roof supplies its column height.
            continue
        tx0, ty0 = np.maximum(np.floor(tri[:, :2].min(0)).astype(int), [x0, y0])
        tx1, ty1 = np.minimum(np.ceil(tri[:, :2].max(0)).astype(int), [x1, y1])
        if tx0 >= tx1 or ty0 >= ty1:
            continue
        yy, xx = np.ogrid[ty0:ty1, tx0:tx1]
        xx, yy = xx + 0.5, yy + 0.5
        u = ((by - cy) * (xx - cx) + (cx - bx) * (yy - cy)) / den
        v = ((cy - ay) * (xx - cx) + (ax - cx) * (yy - cy)) / den
        inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1.000001)
        z = u * az + v * bz + (1 - u - v) * cz
        window = surface[ty0 - y0:ty1 - y0, tx0 - x0:tx1 - x0]
        np.fmax(window, np.where(inside, z, np.nan), out=window)
    return x0, y0, surface


def raster_triangles(paths):
    """Highest surface intersecting each cell centre, with barycentric z interpolation (tiles in parallel)."""
    surface = np.full((D, W), np.nan, np.float32)
    with Pool(max(1, min(len(paths), (os.cpu_count() or 2) // 2))) as pool:
        for result in pool.imap_unordered(_raster_file, paths):
            if result is not None:
                x0, y0, part = result
                window = surface[y0:y0 + part.shape[0], x0:x0 + part.shape[1]]
                np.fmax(window, part, out=window)
    return surface


def grid_points(geometry):
    valid = [p for p in geometry if "lon" in p and "lat" in p]
    if len(valid) != len(geometry) or len(valid) < 2:
        return []
    x, y = PROJECT.transform([p["lon"] for p in valid], [p["lat"] for p in valid])
    return list(zip(np.asarray(x) - WEST, NORTH - np.asarray(y)))


def osm_features():
    data = json.loads(ghent_area.OSM.read_text())
    features = []
    for item in data["elements"]:
        tags = item.get("tags", {})
        if item["type"] == "way":
            pts = grid_points(item.get("geometry", []))
            if len(pts) < 2:
                continue
            shape = Polygon(pts) if len(pts) >= 4 and pts[0] == pts[-1] else LineString(pts)
        elif item["type"] == "relation":
            outer, inner = [], []
            for member in item.get("members", []):
                pts = grid_points(member.get("geometry", []))
                if len(pts) >= 2:
                    (inner if member.get("role") == "inner" else outer).append(LineString(pts))
            if not outer:
                continue
            shape = unary_union(list(polygonize(unary_union(outer))))
            if inner:
                shape = shape.difference(unary_union(list(polygonize(unary_union(inner)))))
        else:
            continue
        if not shape.is_valid:
            shape = shape.buffer(0)
        shape = shape.intersection(AREA)
        if not shape.is_empty:
            features.append((tags, shape))
    return features, data.get("osm3s", {}).get("timestamp_osm_base", "unknown")


def paint(target, shapes):
    if shapes:
        rasterize(shapes, out=target, transform=Affine.identity())


def stats(ids, count, cost=False):
    slices = ndimage.find_objects(ids, count)
    sizes = np.bincount(ids.ravel(), minlength=count + 1)
    result = []
    for i, region in enumerate(slices, 1):
        if region is None:
            raise ValueError(f"Missing region {i}")
        rows, cols = region
        result.append([(cols.start + cols.stop) / 2, (rows.start + rows.stop) / 2,
                       int(sizes[i]), cols.start, rows.start, cols.stop, rows.stop]
                      + ([int(sizes[i] * 8)] if cost else []))
    return result


BRICKS = [(128, 76, 62), (116, 70, 58), (138, 84, 68), (104, 64, 54), (146, 94, 74), (124, 80, 66),
          (112, 72, 60), (132, 78, 62)]
DARK_BRICKS = [(84, 56, 50), (96, 62, 54), (74, 52, 48)]
STONES = [(196, 176, 140), (184, 164, 128), (150, 146, 138), (172, 160, 138)]
PLASTERS = [(222, 214, 196), (210, 200, 180), (228, 206, 170), (214, 190, 160), (200, 214, 206)]
HALLS = [(150, 148, 140), (138, 136, 130), (160, 154, 142)]  # Doornik limestone and sandstone, weathered grey
MODERN = [(196, 190, 178), (176, 174, 168), (206, 200, 186), (160, 160, 156)]
M_WALL, M_TENEMENT, M_STONE, M_BRICK = 1, 22, 23, 24


def building_walls(labels, count, rel_roof, castle_cells):
    """Per building: facade colour and material.  Traditional buildings (pitched roofs, or small and low) get
    masonry -- brick mostly, some stone, some plaster, as in a Flemish town -- and big flat ones get a plain modern
    wall.  The Gravensteen is ashlar stone.  Returns ((count + 1, 3) uint8, (count + 1,) uint8)."""
    flat = labels.ravel()
    n = np.bincount(flat, minlength=count + 1).astype(np.float64)
    r = rel_roof.ravel().astype(np.float64)
    s1 = np.bincount(flat, weights=r, minlength=count + 1)
    s2 = np.bincount(flat, weights=r * r, minlength=count + 1)
    mean = s1 / np.maximum(n, 1)
    std = np.sqrt(np.maximum(s2 / np.maximum(n, 1) - mean * mean, 0))
    peak = ndimage.maximum(rel_roof, labels, np.arange(count + 1))
    # pitched roofs, or the size and height of a town house; big, tall or flat-and-wide is modern
    # Ghent's centre is old unless proven otherwise: modern means a flat roof on something big or tall.  Big pitched
    # buildings (churches, halls, abbeys) are grey ashlar.
    pitched = std >= 0.7
    modern = ~pitched & ((n >= 600) | (peak > 20)) & (peak <= 28)
    hall = (pitched & (n >= 2500)) | (peak > 28)  # and every tower: the belfry, the church towers
    old = ~modern & ~hall
    colour = np.zeros((count + 1, 3), np.uint8)
    material = np.full(count + 1, M_WALL, np.uint8)
    rng = np.random.default_rng(1)  # one fixed draw per building label: stable across rebuilds
    pick = rng.random(count + 1)
    index = rng.integers(0, 1 << 20, count + 1)
    for i in range(1, count + 1):
        if old[i]:
            p = pick[i]
            if p < 0.55:
                colour[i], material[i] = BRICKS[index[i] % len(BRICKS)], M_BRICK
            elif p < 0.67:
                colour[i], material[i] = DARK_BRICKS[index[i] % len(DARK_BRICKS)], M_BRICK
            elif p < 0.85:
                colour[i], material[i] = STONES[index[i] % len(STONES)], M_BRICK
            else:
                colour[i], material[i] = PLASTERS[index[i] % len(PLASTERS)], M_TENEMENT
        elif hall[i]:
            colour[i], material[i] = HALLS[index[i] % len(HALLS)], M_STONE
        else:
            colour[i] = MODERN[index[i] % len(MODERN)]
    cells = n[1:].sum()
    note("facades by cells: old", round(float(n[1:][old[1:]].sum() / cells), 3), "hall", round(float(n[1:][hall[1:]].sum() / cells), 3),
         "modern", round(float(n[1:][modern[1:]].sum() / cells), 3))
    if castle_cells is not None and castle_cells.any():
        for i in np.unique(labels[castle_cells]):
            if i:
                colour[i], material[i] = (146, 140, 128), M_STONE
    return colour, material


def grade(rgb, lift=1.20, offset=12, soften=0.10):
    """Lift the dark 2021 ortho and soften it toward warm stone, as Warsaw does."""
    x = rgb.astype(np.float32) * lift + offset
    x = x * (1 - soften) + np.array([150, 146, 132], np.float32) * soften
    # The 2021 mosaic leans violet in shade: where red and blue both beat green (and blue is not far below red, so
    # terracotta stays terracotta) pull the magenta excess out.
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    cast = np.clip((r + b) / 2 - g, 0, None) * (b > 0.72 * r)
    x[..., 0] = r - 0.85 * cast
    x[..., 2] = b - 0.85 * cast
    return np.clip(x, 0, 255).astype(np.uint8)


# What Ghent's roofs are made of, as the graded ortho shows them: slate, red and dark tile, bitumen/gravel, zinc.
ROOF_KINDS = np.array([[84, 88, 96], [156, 86, 60], [104, 74, 62], [128, 124, 116], [150, 152, 150]], np.float32)


def coherent_roofs(rgb, labels, count, buildings, own=0.55, pull=0.35):
    """A roof is one material: blend each cell toward its building's mean tone, itself pulled toward the nearest roof
    kind, keeping some of the local (5 x 5 median) photo for the light and dark sides and patched repairs."""
    local = ndimage.median_filter(rgb, size=(5, 5, 1)).astype(np.float32)
    flat = labels[buildings]
    n = np.maximum(np.bincount(flat, minlength=count + 1), 1).astype(np.float32)
    lb = local[buildings]
    mean = np.stack([np.bincount(flat, weights=lb[:, k], minlength=count + 1) / n for k in range(3)], 1)
    nearest = ROOF_KINDS[np.argmin(((mean[:, None, :] - ROOF_KINDS[None]) ** 2).sum(2), axis=1)]
    tone = mean * (1 - pull) + nearest * pull
    out = rgb.copy()
    out[buildings] = np.clip(tone[flat] * own + lb * (1 - own), 0, 255).astype(np.uint8)
    return out


def palette_ids(rgb, material, base, cap=4096):
    """Quantize visible colours + material ids into palette indices."""
    rgb = np.asarray(rgb, np.uint8).reshape(-1, 3)
    material = np.asarray(material, np.uint8).reshape(-1)
    if len(rgb) != len(material):
        raise ValueError("colour and material arrays differ in length")
    def keys(rgb_part, material_part, q):
        qc = (rgb_part.astype(np.uint16) // q) * q
        return ((qc[:, 0].astype(np.uint32) << 24) | (qc[:, 1].astype(np.uint32) << 16)
                | (qc[:, 2].astype(np.uint32) << 8) | material_part.astype(np.uint32))
    for q in (4, 6, 8, 10, 12, 16, 24, 32):
        if len(np.unique(keys(rgb[::13], material[::13], q))) + len(base) > cap:
            continue  # a subsample already overflows: skip the full pass
        unique, inverse = np.unique(keys(rgb, material, q), return_inverse=True)
        if len(unique) + len(base) <= cap:
            break
    else:
        raise RuntimeError("ortho colours cannot fit the 64 x 64 palette")
    note("palette quantisation step", q)
    entries = [list(entry) for entry in base]
    for value in unique:
        entries.append([int((value >> 24) & 255), int((value >> 16) & 255),
                        int((value >> 8) & 255), int(value & 255)])
    return entries, (inverse + len(base)).astype(np.uint16)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dump("georeference.json", make_georeference(WEST, NORTH, W, D))
    terrain_paths = [RAW / f"Trn_{x}_{y}_10_0_N_2013.stl" for x, y in ghent_area.stl_tiles()]
    building_paths = [RAW / f"Geb_{x}_{y}_10_2_N_2013.stl" for x, y in ghent_area.stl_tiles()]
    terrain_paths, building_paths = [p for p in terrain_paths if p.exists()], [p for p in building_paths if p.exists()]
    if not terrain_paths or not building_paths:
        raise RuntimeError("Run scripts/fetch_ghent.py first (GHENT_AREA=%s)" % ghent_area.NAME)

    note("official rasters")
    dsm, dtm = vlaanderen.read_surfaces()
    ortho = vlaanderen.read_ortho()
    dsm = np.where(dsm > -100, dsm, dtm)
    dtm = np.where(dtm > -100, dtm, np.nan)
    dtm_ok = np.isfinite(dtm)
    if not np.isfinite(dtm).all():
        # nodata in the DTM (water, a block edge): take the nearest bare earth
        hole = ~np.isfinite(dtm)
        note("dtm nodata cells", int(hole.sum()))
        nearest = ndimage.distance_transform_edt(hole, return_distances=False, return_indices=True)
        dtm = dtm[tuple(nearest)]
        dsm = np.where(np.isfinite(dsm), dsm, dtm)
        del hole, nearest

    note("terrain")
    ground_stl = raster_triangles(terrain_paths)
    stl_valid = np.isfinite(ground_stl)
    coverage = float(stl_valid.mean())
    # The city's STL doesn't reach every corner of the ring: the measured DHMV II DTM fills in. Only ground that
    # neither of them measured would be invented.
    measured = float((stl_valid | dtm_ok).mean())
    del dtm_ok
    note("terrain: stl", round(coverage, 3), "stl or dtm", round(measured, 3))
    if measured < 0.95:
        raise RuntimeError(f"Measured terrain covers only {measured:.1%}; refusing to invent the ground")
    ground = np.where(stl_valid, ground_stl, dtm).astype(np.float64)
    dtm_fill = float((~stl_valid).mean())
    # A handful of STL holes may still fall on DTM nodata; fill those locally.
    if not np.isfinite(ground).all():
        nearest = ndimage.distance_transform_edt(~np.isfinite(ground), return_distances=False, return_indices=True)
        ground = ground[tuple(nearest)]
    terrain_delta = float(np.nanmedian(ground[stl_valid] - dtm[stl_valid]))
    base = math.floor(float(ground.min())) - 2
    g = np.maximum(2, np.rint(ground - base)).astype(np.uint8)

    note("buildings")
    # GRB is only a 2-D footprint mask ("this X/Y is an official building").
    # Heights come from DHMV II (DSM), whose roofs are stepped, not LOD1 boxes.
    # The STL only adds buildings newer than the GRB/DHMV (e.g. De Krook).
    note("grb footprints")
    grb = ghent_grb.load()
    grb_mask, footprint = grb["mask"], grb["footprint"]
    del grb
    grb_mask = ndimage.binary_closing(grb_mask, iterations=1)
    dsm_building = grb_mask & ((dsm - ground) > 2)
    roof_stl = raster_triangles(building_paths)
    stl_new = np.isfinite(roof_stl) & (roof_stl > ground + 2) & ~ndimage.binary_dilation(grb_mask, iterations=2)
    buildings = dsm_building | stl_new
    roof = np.where(dsm_building, dsm, np.where(stl_new, roof_stl, np.nan))
    top = np.where(buildings, np.ceil(roof - base), g).astype(np.uint16)
    grb_share = float(dsm_building.sum() / max(1, int(buildings.sum())))

    # The Gravensteen has a real model: it replaces the city's rough block inside the castle's outline.
    castle = ghent_castle.place(W, D, WEST, NORTH, ground)
    castle_zone = castle_cells = None
    if castle is not None:
        castle_zone, castle_cells, castle_roof = castle
        buildings = (buildings & ~castle_zone) | castle_cells
        roof = np.where(castle_cells, castle_roof, np.where(castle_zone, np.nan, roof))
        top = np.where(buildings, np.ceil(roof - base), g).astype(np.uint16)
        note("gravensteen:", int(castle_cells.sum()), "building cells, roof up to", round(float(np.nanmax(castle_roof)), 1), "m")

    # LiDAR roofs are rough at 1 m.  A 3 x 3 median inside each roof keeps pitches (a median of a ramp is the ramp) but
    # drops the speckle; it stays off roof edges, and off anything it would move by more than 2.5 m (spires, chimneys).
    inner = buildings & (ndimage.uniform_filter(buildings.astype(np.float32), 3) > 0.99)
    if castle_cells is not None:
        inner &= ~castle_cells
    med = ndimage.median_filter(np.where(buildings, roof, ground).astype(np.float32), size=3)
    calm = inner & (np.abs(med - roof) < 2.5)
    roof = np.where(calm, med, roof)
    top = np.where(buildings, np.ceil(roof - base), g).astype(np.uint16)
    del med, inner
    note("roof smoothing:", int(calm.sum()), "cells")

    note("osm")
    features, timestamp = osm_features()
    surface = np.zeros((D, W), np.uint8)  # material ids: 0 paved/bare, 4 water, 8 grass
    green, water, roads, paths = [], [], [], []
    places, label_candidates = [], []
    for tags, shape in features:
        polygon = shape.geom_type in ("Polygon", "MultiPolygon")
        if polygon and (tags.get("landuse") in ("grass", "forest", "meadow", "recreation_ground")
                        or tags.get("leisure") in ("park", "garden") or tags.get("natural") in ("wood", "grassland", "scrub")):
            green.append((shape, GRASS))
        if polygon and (tags.get("natural") == "water" or "water" in tags or tags.get("waterway") == "riverbank"):
            water.append((shape, WATER))
        highway = tags.get("highway")
        if highway and not polygon and tags.get("tunnel") != "yes" and tags.get("bridge") in (None, "no"):
            pedestrian = highway in ("footway", "path", "pedestrian", "steps", "cycleway")
            widths = {"primary": 6, "secondary": 5, "tertiary": 4, "residential": 3, "service": 2}
            width = 2 if pedestrian else widths.get(highway, 3)
            (paths if pedestrian else roads).append((shape.buffer(width), 1))
        name = tags.get("name:nl") or tags.get("name")
        if name:
            p = shape.representative_point()
            kind = tags.get("tourism") or tags.get("historic") or tags.get("amenity") or highway or "place"
            places.append([name, kind, round(p.x, 1), round(p.y, 1), 40, tags.get("name:en", "")])
            if polygon and (tags.get("historic") == "castle" or tags.get("building") in ("church", "cathedral")
                            or any(s in name.lower() for s in ("belfort", "gravensteen", "sint-baafs", "sint-niklaas", "vrijdagmarkt", "graslei"))):
                label_candidates.append((name, kind, p.x, p.y, tags))
    paint(surface, green)
    paint(surface, water)
    paved = np.zeros((D, W), np.uint8)
    paint(paved, roads)
    paint(paved, paths)
    paved = paved.astype(bool)

    note("bridges")
    bridges = ghent_bridges.load(ghent_area.OSM_BRIDGES, WEST, NORTH, W, D)
    deck, bridge_zone = ghent_bridges.build(bridges, dsm, ground)
    deck_cells = np.isfinite(deck)
    removed = buildings & bridge_zone & ~deck_cells
    buildings &= ~(bridge_zone | deck_cells)
    top = np.where(buildings, top, g).astype(np.uint16)
    deck_top = np.where(deck_cells, np.ceil(np.where(deck_cells, deck, 0) - base), 0).astype(np.uint16)
    # a parapet runs along the deck edges that look down on something, not where the road carries on
    lower = np.zeros((D, W), bool)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        n_deck = np.roll(deck_cells, (dy, dx), (0, 1))
        n_ground = np.roll(ground, (dy, dx), (0, 1))
        lower |= ~n_deck & (n_ground < np.where(deck_cells, deck, 0) - 1.0)
    parapet = deck_cells & lower
    note("bridges:", len(bridges), "found,", int(deck_cells.sum()), "deck cells,", int(removed.sum()), "solid cells removed")

    note("building classes")
    if castle_cells is not None:  # the castle is its own building, whatever touches it
        bld_id, nbld = ndimage.label(buildings & ~castle_cells)
        castle_id, ncastle = ndimage.label(castle_cells)
        bld_id[castle_cells] = castle_id[castle_cells] + nbld
        nbld += ncastle
    else:
        bld_id, nbld = ndimage.label(buildings)
    # Facades and roofs go by house: the GRB footprint where there is one, else the connected building (newer STL
    # buildings, the castle).  Whole terraced blocks are one connected region, and must not share one facade.
    gap = footprint == 0  # cells the mask's closing filled in: take the nearest footprint within 3 m
    dist, (iy, ix) = ndimage.distance_transform_edt(gap, return_indices=True)
    near = footprint[iy, ix]
    del iy, ix
    house = np.where(buildings & (~gap | (dist <= 3)), np.where(gap, near, footprint), 0).astype(np.int64)
    del dist, near, gap
    if castle_cells is not None:
        house[castle_cells] = 0
    house = np.where(buildings & (house == 0), bld_id.astype(np.int64) + int(footprint.max()) + 1, house)
    _, inverse = np.unique(house, return_inverse=True)
    house = inverse.reshape(D, W).astype(np.int32)  # 0 stays 0 (not a building): it is the smallest value
    nhouse = int(house.max())
    note("houses:", nhouse)
    walls_table, wall_mat_table = building_walls(house, nhouse, np.where(buildings, roof - ground, 0.0), castle_cells)

    note("colours")
    # Median over 3 x 3 cells keeps roof tiles instead of individual leaves.
    clean = ndimage.median_filter(ortho, size=(3, 3, 1))
    ground_rgb = grade(clean)
    grass = surface == GRASS
    # The 2021 mosaic is unusually desaturated; keep its texture but restore a
    # summer green so parks do not read as beige pavement.
    if grass.any():
        pixels = ground_rgb[grass].astype(np.float32)
        light = pixels.mean(1, keepdims=True) / 110.0
        tint = np.array([88, 122, 70], np.float32) * np.clip(light, 0.35, 1.6)
        ground_rgb[grass] = np.clip(pixels * 0.45 + tint * 0.55, 0, 255).astype(np.uint8)
    ground_rgb[surface == WATER] = (ground_rgb[surface == WATER].astype(np.float32) * 0.55
                                    + np.array([88, 112, 116], np.float32) * 0.45).astype(np.uint8)
    desat = paved.astype(np.float32)[..., None]
    luma = (ground_rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
    ground_rgb = (ground_rgb * (1 - 0.30 * desat) + luma * 0.30 * desat).astype(np.uint8)

    # Roof colour comes from the orthophoto; facades are a darker tone of it.
    roof_rgb = coherent_roofs(grade(clean), house, nhouse, buildings)
    wall_rgb = np.zeros_like(roof_rgb)
    wall_rgb[buildings] = walls_table[house[buildings]]
    wall_mat = wall_mat_table[house[buildings]]
    # Inside one house a roof step is still roof: the band reaches down to the lower roof beside it, so a 4 m drop in a
    # lumpy roof doesn't open a strip of facade (and its windows) in the middle of the roof.  Outer walls keep theirs.
    band = np.full((D, W), 2, np.int32)
    ti = top.astype(np.int32)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        same = buildings & (np.roll(house, (dy, dx), (0, 1)) == house)
        band = np.where(same, np.maximum(band, ti - np.roll(ti, (dy, dx), (0, 1))), band)
    roof_band = np.clip(band, 2, 40).astype(np.uint8)
    del house, band, ti
    # One palette for every visible colour: ground, roofs, facades and bridges.
    count = int(buildings.sum())
    ndeck, npar = int(deck_cells.sum()), int(parapet.sum())
    deck_rgb = ground_rgb[deck_cells].astype(np.float32)
    rgb_all = np.concatenate([ground_rgb.reshape(-1, 3),
                              roof_rgb[buildings],
                              wall_rgb[buildings],
                              deck_rgb.astype(np.uint8),
                              (deck_rgb * 0.62).astype(np.uint8),
                              np.tile(np.array([[168, 162, 150]], np.uint8), (npar, 1))])
    mat_all = np.concatenate([surface.ravel(),
                              np.full(count, 12, np.uint8),
                              wall_mat,
                              np.zeros(ndeck * 2 + npar, np.uint8)])
    palette, ids = palette_ids(rgb_all, mat_all, PALETTE)
    ground_ids = ids[:D * W].reshape(D, W)
    roof_ids = np.zeros((D, W), np.uint16)
    wall_ids = np.zeros((D, W), np.uint16)
    roof_ids[buildings] = ids[D * W:D * W + count]
    wall_ids[buildings] = ids[D * W + count:D * W + 2 * count]
    deck_top_ids, deck_low_ids, parapet_ids = (np.zeros((D, W), np.uint16) for _ in range(3))
    k = D * W + 2 * count
    deck_top_ids[deck_cells], deck_low_ids[deck_cells] = ids[k:k + ndeck], ids[k + ndeck:k + 2 * ndeck]
    parapet_ids[parapet] = ids[k + 2 * ndeck:]
    grass_ids = tuple(int(v) for v in np.unique(ground_ids[grass])) if grass.any() else ()
    note("palette", len(palette), "entries;", len(grass_ids), "grass ids")

    note("canopy")
    inventory = json.loads(ghent_area.TREES.read_text())
    tree_points = []
    for tree in inventory["trees"]:
        east, north = PROJECT.transform(tree["lon"], tree["lat"])
        x, y = round(east - WEST), round(NORTH - north)
        if 0 <= x < W and 0 <= y < D:
            tree_points.append((x, y))
    tree_points = np.array(tree_points, dtype=int).reshape(-1, 2)
    canopy = np.zeros((D, W), bool)
    pad = 24  # canopy filters look a few cells around: work in 2 km blocks with a margin
    for r0 in range(0, D, 2000):
        for c0 in range(0, W, 2000):
            a0, a1, b0, b1 = max(0, r0 - pad), min(D, r0 + 2000 + pad), max(0, c0 - pad), min(W, c0 + 2000 + pad)
            inside = tree_points[(tree_points[:, 0] >= b0) & (tree_points[:, 0] < b1) & (tree_points[:, 1] >= a0) & (tree_points[:, 1] < a1)]
            part = ghent_canopy.canopy_mask(dsm[a0:a1, b0:b1], dtm[a0:a1, b0:b1], buildings[a0:a1, b0:b1],
                                            ortho[a0:a1, b0:b1], inside - [b0, a0])
            r1, c1 = min(D, r0 + 2000), min(W, c0 + 2000)
            canopy[r0:r1, c0:c1] = part[r0 - a0:r1 - a0, c0 - b0:c1 - b0]
    if castle_zone is not None:
        canopy &= ~castle_zone
    canopy &= ~(deck_cells | bridge_zone)
    canopy = ndimage.binary_opening(canopy)  # 1-cell slivers along roofs and walls would stand as green pillars
    components = int(ndimage.label(canopy)[1])
    canopy_peak = int(np.ceil(float(dsm[canopy].max()) - base)) if canopy.any() else 0
    H = max(128, int(top.max()) + 8, canopy_peak + 2, int(deck_top.max()) + 3)
    note("ground coverage", coverage, "dtm fill", dtm_fill, "datum", base, "volume", (H, D, W))

    note("trees")
    trees, schematic = [], []
    tree_skips = {"outside": 0, "building": 0, "canopy": 0}
    for tree in inventory["trees"]:
        east, north = PROJECT.transform(tree["lon"], tree["lat"])
        x, y = round(east - WEST), round(NORTH - north)
        if not (0 <= x < W and 0 <= y < D):
            tree_skips["outside"] += 1
            continue
        if buildings[y, x]:
            tree_skips["building"] += 1
            continue
        y0, y1, x0, x1 = max(0, y - 4), min(D, y + 5), max(0, x - 4), min(W, x + 5)
        if canopy[y0:y1, x0:x1].any():
            # The DSM crown already draws this tree; record its measured shape.
            window = canopy[y0:y1, x0:x1]
            height = int(round(float(np.max(dsm[y0:y1, x0:x1][window] - dtm[y0:y1, x0:x1][window]))))
            cells = int(window.sum())
            tree_skips["canopy"] += 1
            trees.append(dict(tree, x=x, y=y, rendered_height_m=max(2, height),
                              crown_radius_estimate_m=max(2, round(math.sqrt(cells / math.pi))),
                              height_inferred=False, rendered_from="dhsm"))
            continue
        # No 2013-15 crown here: a recently planted or pruned tree. Keep the
        # schematic crown from the city's height class.
        height = tree["height_estimate_m"] or (5 if tree["height_class"] == "<6 m." else 26 if tree["height_class"] == "> 24 m." else 9)
        h = int(round(height))
        radius = max(2, min(6, int(h * 0.24)))
        schematic.append((x, y, h, radius, int(g[y, x])))
        trees.append(dict(tree, x=x, y=y, rendered_height_m=h, crown_radius_estimate_m=radius,
                          height_inferred=tree["height_estimate_m"] is None, rendered_from="schematic"))
    dump("trees.json", {k: v for k, v in inventory.items() if k != "trees"} | {"count": len(trees), "skipped_render": tree_skips, "trees": trees})
    schematic = np.array(schematic, dtype=int).reshape(-1, 5)

    def chunk_volume(r0, r1, c0, c1):
        """The 1 m voxels of rows r0..r1, cols c0..c1 of the map: ground, buildings, DSM crowns, schematic crowns."""
        sub = (slice(r0, r1), slice(c0, c1))
        h, w = r1 - r0, c1 - c0
        gw, bw, tw = g[sub], buildings[sub], top[sub]
        vol = np.zeros((H, h, w), np.uint16)
        for z in range(int(gw.max())):
            vol[z] = np.where(z < gw - 1, SOIL, np.where(z == gw - 1, ground_ids[sub], 0))
        walls_w, roofs_w, rb = wall_ids[sub], roof_ids[sub], roof_band[sub].astype(np.int32)
        for z in range(int(gw.min()), int(tw.max())):
            mask = bw & (z >= gw) & (z < tw)
            vol[z, mask] = walls_w[mask]
            is_roof = mask & (z >= tw - rb)
            vol[z, is_roof] = roofs_w[is_roof]
        dk = deck_top[sub]
        if dk.any():
            tops, lows, pars, dpar = deck_top_ids[sub], deck_low_ids[sub], parapet_ids[sub], parapet[sub]
            for z in range(int(dk[dk > 0].min()) - 2, int(dk.max()) + 1):
                upper, lower_ = (dk > 0) & (z == dk - 1), (dk > 0) & (z == dk - 2)
                vol[z, upper] = tops[upper]
                vol[z, lower_] = lows[lower_]
                rail = dpar & (z == dk)
                vol[z, rail] = pars[rail]
        cw = canopy[sub]
        if cw.any():
            ctop = np.where(cw, np.clip(np.ceil(dsm[sub] - base), 0, 65000), 0).astype(np.int32)
            gi = gw.astype(np.int32)
            # A crown is a dome, not a column: thick deep inside the canopy, thin at its rim.
            inset = ndimage.distance_transform_edt(cw)
            thick = np.clip(1.5 + 1.7 * inset, 2, np.maximum(2, 0.75 * (ctop - gi))).astype(np.int32)
            start = np.maximum(gi + 1, ctop - thick)
            # Trunks only under crown centres, where the canopy is deepest.
            centre = cw & (inset >= 2.5) & (inset >= ndimage.maximum_filter(inset, size=7))
            for z in range(int(gw[cw].min()), int(ctop.max())):
                trunk = centre & (z >= gw) & (z < start)
                leaf = cw & (z >= start) & (z < ctop)
                if trunk.any():
                    vol[z][trunk] = TRUNK
                if leaf.any():
                    # sunlit top, shaded underside, and a plain green between
                    vol[z][leaf] = np.where(z >= ctop[leaf] - 1, 8, np.where(z < start[leaf] + 1, 9, 7))
        for x, y, th, radius, bottom in schematic[(schematic[:, 0] >= c0 - 8) & (schematic[:, 0] < c1 + 8)
                                                  & (schematic[:, 1] >= r0 - 8) & (schematic[:, 1] < r1 + 8)]:
            lx, ly = x - c0, y - r0
            ca, cb, ra, rb = max(0, lx - radius), min(w, lx + radius + 1), max(0, ly - radius), min(h, ly + radius + 1)
            if ca >= cb or ra >= rb:
                continue
            rr, cc = np.ogrid[ra:rb, ca:cb]
            for z in range(bottom, min(H, bottom + th)):
                if z < bottom + th * 0.7 and 0 <= lx < w and 0 <= ly < h and vol[z, ly, lx] == 0:
                    vol[z, ly, lx] = TRUNK
                dz = (z - (bottom + th * 0.68)) / max(2, th * 0.35)
                crown = ((cc - lx) ** 2 + (rr - ly) ** 2) / radius ** 2 + dz ** 2 <= 1
                win = vol[z, ra:rb, ca:cb]
                win[crown & (win == 0)] = 7 + (x + y) % 3
        return vol

    bld, count = bld_id, nbld
    if count >= 65535:
        raise RuntimeError("Too many building regions for uint16")
    bld = bld.astype(np.uint16)
    blocks = ((np.arange(D)[:, None] // 128) * math.ceil(W / 128) + np.arange(W)[None, :] // 128 + 1).astype(np.uint16)
    gz("zones.bin", blocks.tobytes() + bld.tobytes())
    dump("zones.json", {"blocks": stats(blocks, int(blocks.max()), True), "buildings": stats(bld, count)})
    gz("terrain.bin", (g[::4, ::4].astype(np.uint16) * 10).tobytes() + ground_rgb[::2, ::2].tobytes())
    labels, views, seen = [], {}, set()
    for name, kind, x, y, tags in sorted(label_candidates, key=lambda item: ("belfort" not in item[0].lower(), item[0])):
        if name in seen:
            continue
        seen.add(name)
        c, r = int(x), int(y)
        z = int(top[max(0, r - 20):min(D, r + 21), max(0, c - 20):min(W, c + 21)].max())
        labels.append(dict(name=name, kind=kind, x=round(x), y=round(y), z=z, g=int(g[r, c]), R=45,
                           en=tags.get("name:en", ""), site=tags.get("website", ""), far=2500))
        views[f"place{len(views)}"] = dict(label=name, pos=[round(x - 180), z + 140, round(y + 260)], target=[round(x), z * 0.45, round(y)])
    bx, by = PROJECT.transform(3.7249, 51.0536)
    bx, by = round(bx - WEST), round(NORTH - by)
    # The opening shot: low over the Leie from the west, the three towers (Sint-Niklaas, Belfort, Sint-Baafs) in a row.
    hx, hy = round(BELFORT[0] - WEST), round(NORTH - BELFORT[1])
    views = {"overview": dict(label="Ghent · three towers", pos=[hx - 430, 70, hy + 250], target=[hx + 10, 40, hy]),
             "aerial": dict(label="Ghent · historic centre", pos=[bx - 580, 820, by + 860], target=[bx, 18, by]), **views}
    manifest = dict(N=tiles.N, levels=[], bytes=0)
    chunks = [(r, c) for r in range(0, D, CHUNK) for c in range(0, W, CHUNK)]
    for n, (cr, cc0) in enumerate(chunks, 1):
        r0, r1, c0, c1 = max(0, cr - HALO), min(D, cr + CHUNK + HALO), max(0, cc0 - HALO), min(W, cc0 + CHUNK + HALO)
        vol = chunk_volume(r0, r1, c0, c1)
        part = tiles.export(vol, g, OUT / "tiles", lambda *a: None, fol_ids=(TRUNK,) + LEAF_IDS,
                            grass_ids=grass_ids, bare_id=SOIL, skip_ids=(TRUNK,) + LEAF_IDS,
                            origin=(r0, c0), owned=(cr, min(D, cr + CHUNK), cc0, min(W, cc0 + CHUNK)))
        manifest["levels"], manifest["bytes"] = part["levels"], manifest["bytes"] + part["bytes"]
        del vol
        note(f"tiles: chunk {n}/{len(chunks)} (rows {cr}-{min(D, cr + CHUNK)}, cols {cc0}-{min(W, cc0 + CHUNK)})")
    dump("meta.json", dict(W=W, D=D, H=H, base=base, theta_deg=0, crs="EPSG:31370", origin=[WEST, NORTH],
                            tiles=manifest, palette=palette, lamps=[], labels=labels, views=views,
                            sources={"osm": timestamp, "geometry": "Stad Gent Gent in 3D via Fly Over Ghent (current STL)",
                                     "trees": inventory["source"], "trees_modified": inventory["source_modified"],
                                     "elevation": "DHMV II DSM/DTM 1 m, Digitaal Vlaanderen WCS, 2013-2015",
                                     "building_footprints": "3D GRB LOD1 KML footprints, used only as a 2-D building mask",
                                     "orthophoto": "Summer orthophoto mosaic 2021 (OMZ RGB), Digitaal Vlaanderen",
                                     "gravensteen": ghent_castle.CREDIT + ", https://www.thingiverse.com/thing:4187726"}))
    dump("places.json", {"places": list({p[0]: p for p in places}.values())})
    dump("traffic.json", {"roads": [], "peds": [], "ctrls": [], "trams": [], "trains": []})
    dump("build-report.json", {
        "terrain_coverage": coverage, "terrain_dtm_fill": dtm_fill, "terrain_stl_vs_dtm_median_m": terrain_delta,
        "buildings": count, "building_cells": int(buildings.sum()), "grb_footprint_share": round(grb_share, 3),
        "trees": len(trees), "tree_skips": tree_skips, "canopy_cells": int(canopy.sum()), "canopy_components": components,
        "palette_entries": len(palette), "labels": len(labels),
        "bounds_lambert72": [WEST, NORTH - D, WEST + W, NORTH],
        "colour_mode": "2021 summer orthophoto; GRB footprints as building mask, DHMV II DSM heights",
        "limitations": [
            "DHMV II is the 2013-2015 LiDAR; a 2013-15 crown that has since been cut is still drawn, and trees planted after 2021 only have a schematic crown.",
            "3D GRB LOD1 is used only as a 2-D footprint mask; its flat-roof heights and legend colours are ignored. Roof heights come from the DHMV II DSM.",
            "Buildings newer than the 2013-2016 data (e.g. De Krook) fall back to the STL roof.",
            "Vertical fill below roofs approximates overhangs and tunnels.",
            "Ground and roofs use the 2021 summer orthophoto (one tone per GRB house); facades are a modelled material per house (brick, stone, plaster or modern), not street-level imagery.",
        ]})
    note("done:", OUT, count, "building regions,", len(trees), "trees,", int(canopy.sum()), "canopy cells,", len(labels), "labels")


if __name__ == "__main__":
    main()
