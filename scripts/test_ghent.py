"""Focused checks for geometry and inventory conversion; run with unittest discovery."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import build_ghent
from delijn_routes import extract_routes
from ghent_georeference import basis, make_georeference
from build_delijn_schedule import seconds, shape_chunks, stop_distances
from ghent_trees import height_range, normalize


class GhentDataTests(unittest.TestCase):
    def test_schedule_times_and_ordered_stops_around_a_loop(self):
        self.assertEqual(seconds("25:10:05"), 90605)
        self.assertIsNone(seconds(""))
        with self.assertRaises(ValueError):
            seconds("25:61:00")
        points = np.array([[0, 0, 0], [100, 0, 100], [100, 100, 200], [0, 100, 300], [0, 0, 400], [100, 0, 500]], dtype=float)
        stops = np.array([[0, 0], [100, 100], [0, 0], [100, 0]])
        self.assertEqual(stop_distances(points, stops), [0, 200, 400, 500])
        self.assertIsNone(stop_distances(points, np.array([[9999, 9999]])))

    def test_shape_clipping_preserves_separate_visits(self):
        points = np.array([[0, 0, 0], [200, 0, 200], [200, 200, 400], [0, 200, 600], [0, 0, 800]], dtype=float)
        chunks = shape_chunks(points, (-10, -10, 100, 100))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][-1][2], 200)
        self.assertEqual(chunks[1][0][2], 600)

    def test_live_transit_projection_matches_lambert72(self):
        geo = make_georeference()
        points = np.array([[3.7249, 51.0536], [3.72, 51.06], [3.737, 51.05]])
        x, y = build_ghent.PROJECT.transform(points[:, 0], points[:, 1])
        design = basis(points[:, 0], points[:, 1])
        np.testing.assert_allclose(design @ geo["x"], x - build_ghent.WEST, atol=0.01)
        np.testing.assert_allclose(design @ geo["y"], build_ghent.NORTH - y, atol=0.01)

    def test_static_route_names_and_transport_types(self):
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("routes.txt", "route_id,route_short_name,route_long_name,route_type\ntram,T1,Gent,0\nbus,55,Gent,3\nother,,Other,2\n")
            self.assertEqual(extract_routes(path), {"tram": {"name": "T1", "kind": "tram"},
                                                   "bus": {"name": "55", "kind": "bus"},
                                                   "other": {"name": "Other", "kind": "vehicle"}})

    def test_height_classes_preserve_uncertainty(self):
        self.assertEqual(height_range("18-24 m."), [18, 24])
        self.assertEqual(height_range("6,5–9 m."), [6.5, 9])
        for value in (None, "<6 m.", "> 24 m.", "24-18 m."):
            self.assertIsNone(height_range(value))

    def test_inventory_filters_invalid_coordinates_and_duplicates(self):
        tree = {"gis_id": "one", "geo_point_2d": {"lon": 3.72, "lat": 51.05},
                "hoogte": "18-24 m.", "sortiment": "Acer", "diameter": 60}
        trees, skipped = normalize([tree, tree, dict(tree, geo_point_2d={"lon": float("nan"), "lat": 51})])
        self.assertEqual(len(trees), 1)
        self.assertEqual(skipped, {"coordinates": 1, "outside_bbox": 0, "duplicate": 1})
        self.assertEqual(trees[0]["height_estimate_m"], 21)
        self.assertEqual(trees[0]["diameter_raw"], 60)
        self.assertEqual(normalize([tree], (3.73, 51, 3.74, 52))[1]["outside_bbox"], 1)
        missing_id = dict(tree, gis_id=None)
        self.assertEqual(normalize([missing_id])[0][0]["id"], normalize([missing_id])[0][0]["id"])

    def test_stl_projection_and_sloped_roof_height(self):
        # z = 10 + x. Check interpolation, north/south flip, and max of overlapping roofs.
        west, north = build_ghent.WEST, build_ghent.NORTH
        triangles = [((west, north, 10), (west + 4, north, 14), (west, north - 4, 10)),
                     ((west, north, 2), (west + 4, north, 2), (west, north - 4, 2))]
        raw = bytearray(80) + struct.pack("<I", len(triangles))
        for vertices in triangles:
            raw += struct.pack("<12fH", 0, 0, 1, *(v for p in vertices for v in p), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.stl"
            path.write_bytes(raw)
            with patch.object(build_ghent, "W", 4), patch.object(build_ghent, "D", 4):
                roof = build_ghent.raster_triangles([path])
            self.assertAlmostEqual(float(roof[0, 0]), 10.5)
            self.assertAlmostEqual(float(roof[0, 2]), 12.5)
            self.assertTrue(np.isnan(roof[3, 3]))

    def test_generated_tiles_match_manifest_and_grid(self):
        import gzip
        output = build_ghent.OUT
        if not (output / "meta.json").exists():
            self.skipTest("Build Ghent first")
        meta = json.loads((output / "meta.json").read_text())
        for level in meta["tiles"]["levels"]:
            for folder in (["1", "1f"] if level["s"] == 1 else [str(level["s"])]):
                for y in range(level["ny"]):
                    for x in range(level["nx"]):
                        payload = gzip.decompress((output / "tiles" / folder / f"{x}_{y}.bin").read_bytes())
                        counts = np.frombuffer(payload, "<u2", count=130 * 130)
                        expected = 130 * 130 * 3 + int(counts.sum()) * 6
                        self.assertGreaterEqual(len(payload), expected)
                        self.assertEqual((len(payload) - expected) % 8, 0)
        width, depth = meta["W"], meta["D"]
        self.assertEqual(len(gzip.decompress((output / "zones.bin").read_bytes())), width * depth * 4)
        trees = json.loads((output / "trees.json").read_text())["trees"]
        self.assertTrue(all(0 <= t["x"] < width and 0 <= t["y"] < depth for t in trees))


if __name__ == "__main__":
    unittest.main()
