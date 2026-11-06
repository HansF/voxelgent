"""Voxelize official Ghent STL roofs/terrain and the municipal tree inventory.

python scripts/fetch_ghent.py && python scripts/ghent_trees.py
python scripts/build_ghent.py

Dependencies: numpy scipy shapely rasterio pyproj (also used by the Warsaw pipeline).
Output is separate from Warsaw: web/public/ghent. Geometry: Lambert 72, 1 m voxels.
STL has no colours: OSM materials and a deterministic illustrative palette supply them.
Buildings are filled vertically under the roof; overhangs and tunnels are approximated.
"""
import gzip
import json
import math
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from scipy import ndimage
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union

import tiles
from ghent_georeference import make_georeference

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/ghent"
OUT = ROOT / "web/public/ghent"
W = D = 2000
WEST, NORTH = 104000, 195000
AREA = box(0, 0, W, D)
PROJECT = Transformer.from_crs(4326, 31370, always_xy=True)
# Palette alpha stores the viewer's material id, not opacity.
PALETTE = [[0, 0, 0, 0], [158, 153, 140, 0], [103, 128, 125, 4],
           [92, 98, 100, 0], [185, 177, 159, 0], [104, 133, 78, 8],
           [101, 70, 48, 0], [76, 116, 58, 3], [91, 132, 63, 3], [64, 101, 54, 3],
           [160, 101, 73, 1], [179, 140, 104, 1], [203, 190, 160, 1],
           [144, 132, 113, 1], [185, 170, 145, 1],
           [118, 78, 60, 12], [94, 100, 103, 12], [159, 106, 75, 12]]


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


def raster_triangles(paths):
    """Highest surface intersecting each cell centre, with barycentric z interpolation."""
    surface = np.full((D, W), np.nan, np.float32)
    for path in paths:
        triangles = read_stl(path)
        note(path.name, len(triangles), "triangles")
        for tri in triangles:
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
            den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(den) < 1e-8:  # Vertical wall: the roof supplies its column height.
                continue
            x0, y0 = np.maximum(np.floor(tri[:, :2].min(0)).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(tri[:, :2].max(0)).astype(int), [W, D])
            if x0 >= x1 or y0 >= y1:
                continue
            yy, xx = np.ogrid[y0:y1, x0:x1]
            xx, yy = xx + 0.5, yy + 0.5
            u = ((by - cy) * (xx - cx) + (cx - bx) * (yy - cy)) / den
            v = ((cy - ay) * (xx - cx) + (ax - cx) * (yy - cy)) / den
            inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1.000001)
            z = u * az + v * bz + (1 - u - v) * cz
            window = surface[y0:y1, x0:x1]
            np.fmax(window, np.where(inside, z, np.nan), out=window)
    return surface


def grid_points(geometry):
    valid = [p for p in geometry if "lon" in p and "lat" in p]
    if len(valid) != len(geometry) or len(valid) < 2:
        return []
    x, y = PROJECT.transform([p["lon"] for p in valid], [p["lat"] for p in valid])
    return list(zip(np.asarray(x) - WEST, NORTH - np.asarray(y)))


def osm_features():
    data = json.loads((RAW / "osm.json").read_text())
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


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dump("georeference.json", make_georeference(WEST, NORTH, W, D))
    terrain_paths, building_paths = sorted(RAW.glob("Trn_*.stl")), sorted(RAW.glob("Geb_*.stl"))
    if len(terrain_paths) != 4 or len(building_paths) != 4:
        raise RuntimeError("Run scripts/fetch_ghent.py first (four building and four terrain tiles required)")
    ground = raster_triangles(terrain_paths)
    valid = np.isfinite(ground)
    coverage = float(valid.mean())
    if coverage < 0.95:
        raise RuntimeError(f"Terrain coverage only {coverage:.1%}; refusing to invent the ground")
    if not valid.all():
        nearest = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
        ground = ground[tuple(nearest)]
    # Absolute source z stays in metadata; voxel heights start at a common integer datum.
    base = math.floor(float(ground.min())) - 2
    g = np.maximum(2, np.rint(ground - base)).astype(np.uint8)
    roof = raster_triangles(building_paths)
    buildings = np.isfinite(roof) & (roof > ground + 2)
    top = np.where(buildings, np.ceil(roof - base), g).astype(np.uint16)
    features, timestamp = osm_features()
    surface = np.full((D, W), 1, np.uint16)
    green, water, roads, paths, facades = [], [], [], [], []
    walls = np.full((D, W), 12, np.uint16)
    roof_colors = np.full((D, W), 16, np.uint16)
    places = []
    label_candidates = []
    for tags, shape in features:
        polygon = shape.geom_type in ("Polygon", "MultiPolygon")
        if polygon and (tags.get("landuse") in ("grass", "forest", "meadow", "recreation_ground")
                        or tags.get("leisure") in ("park", "garden") or tags.get("natural") in ("wood", "grassland", "scrub")):
            green.append((shape, 5))
        if polygon and (tags.get("natural") == "water" or "water" in tags or tags.get("waterway") == "riverbank"):
            water.append((shape, 2))
        highway = tags.get("highway")
        if highway and not polygon and tags.get("tunnel") != "yes":
            pedestrian = highway in ("footway", "path", "pedestrian", "steps", "cycleway")
            widths = {"primary": 6, "secondary": 5, "tertiary": 4, "residential": 3, "service": 2}
            width = 2 if pedestrian else widths.get(highway, 3)
            (paths if pedestrian else roads).append((shape.buffer(width), 4 if pedestrian else 3))
        if polygon and "building" in tags:
            # STL contains shape only. Colours here are illustrative unless supplied by OSM.
            seed = int(shape.centroid.x * 19 + shape.centroid.y * 23)
            facades.append((shape, 10 + seed % 5))
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
    paint(surface, roads)
    paint(surface, paths)
    paint(walls, facades)
    H = max(128, int(top.max()) + 8)
    note("ground coverage", coverage, "datum", base, "volume", (H, D, W))
    vol = np.zeros((H, D, W), np.uint16)
    for z in range(int(g.max())):
        vol[z] = np.where(z < g - 1, 1, np.where(z == g - 1, surface, 0))
    for z in range(int(g.min()), int(top.max())):
        mask = buildings & (z >= g) & (z < top)
        vol[z, mask] = walls[mask]
        is_roof = mask & (z >= top - 2)
        vol[z, is_roof] = roof_colors[is_roof]
    note("trees")
    inventory = json.loads((ROOT / "data/ghent/trees.json").read_text())
    trees = []
    tree_skips = {"outside": 0, "building": 0}
    for tree in inventory["trees"]:
        east, north = PROJECT.transform(tree["lon"], tree["lat"])
        x, y = round(east - WEST), round(NORTH - north)
        if not (0 <= x < W and 0 <= y < D):
            tree_skips["outside"] += 1
            continue
        if buildings[y, x]:
            tree_skips["building"] += 1
            continue
        # No crown measurement is supplied. Derive a schematic crown from estimated height.
        height = tree["height_estimate_m"] or (5 if tree["height_class"] == "<6 m." else 26 if tree["height_class"] == "> 24 m." else 9)
        h = int(round(height))
        radius = max(2, min(6, int(h * 0.24)))
        bottom = int(g[y, x])
        c0, c1, r0, r1 = max(0, x - radius), min(W, x + radius + 1), max(0, y - radius), min(D, y + radius + 1)
        rr, cc = np.ogrid[r0:r1, c0:c1]
        for z in range(bottom, min(H, bottom + h)):
            if z < bottom + h * 0.7 and vol[z, y, x] == 0:
                vol[z, y, x] = 6
            dz = (z - (bottom + h * 0.68)) / max(2, h * 0.35)
            crown = ((cc - x) ** 2 + (rr - y) ** 2) / radius ** 2 + dz ** 2 <= 1
            win = vol[z, r0:r1, c0:c1]
            win[crown & (win == 0)] = 7 + (x + y) % 3
        trees.append(dict(tree, x=x, y=y, rendered_height_m=h, crown_radius_estimate_m=radius,
                          height_inferred=tree["height_estimate_m"] is None))
    dump("trees.json", {k: v for k, v in inventory.items() if k != "trees"} | {"count": len(trees), "skipped_render": tree_skips, "trees": trees})
    bld, count = ndimage.label(buildings)
    if count >= 65535:
        raise RuntimeError("Too many building regions for uint16")
    bld = bld.astype(np.uint16)
    rr, cc = np.indices((D, W))
    blocks = ((rr // 128) * math.ceil(W / 128) + cc // 128 + 1).astype(np.uint16)
    gz("zones.bin", blocks.tobytes() + bld.tobytes())
    dump("zones.json", {"blocks": stats(blocks, int(blocks.max()), True), "buildings": stats(bld, count)})
    palette = np.array(PALETTE, np.uint8)
    rgb = palette[surface[::2, ::2], :3]
    gz("terrain.bin", (g[::4, ::4].astype(np.uint16) * 10).tobytes() + rgb.tobytes())
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
    views = {"overview": dict(label="Ghent · historic centre", pos=[bx - 580, 820, by + 860], target=[bx, 18, by]), **views}
    manifest = tiles.export(vol, g, OUT / "tiles", note, fol_ids=(6, 7, 8, 9), grass_ids=(5,), bare_id=1, skip_ids=(6, 7, 8, 9))
    dump("meta.json", dict(W=W, D=D, H=H, base=base, theta_deg=0, crs="EPSG:31370", origin=[WEST, NORTH],
                            tiles=manifest, palette=PALETTE, lamps=[], labels=labels, views=views,
                            sources={"osm": timestamp, "geometry": "Stad Gent Gent in 3D, 2013; STL conversion by Fly Over Ghent",
                                     "trees": inventory["source"], "trees_modified": inventory["source_modified"]}))
    dump("places.json", {"places": list({p[0]: p for p in places}.values())})
    # Static first release: Warsaw traffic paths cannot be reused in Ghent.
    dump("traffic.json", {"roads": [], "peds": [], "ctrls": [], "trams": [], "trains": []})
    dump("build-report.json", {"terrain_coverage": coverage, "buildings": count, "trees": len(trees),
                                "tree_skips": tree_skips, "labels": len(labels), "bounds_lambert72": [WEST, NORTH - D, WEST + W, NORTH],
                                "limitations": ["2013 geometry; newer buildings are absent.", "Vertical fill below roofs approximates overhangs and tunnels.",
                                                "Illustrative surface and facade colours, not aerial imagery.", "Schematic tree crowns and inferred heights where measurements are missing."]})
    note("done:", OUT, count, "building regions,", len(trees), "trees,", len(labels), "labels")


if __name__ == "__main__":
    main()
