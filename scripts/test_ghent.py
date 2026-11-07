"""Focused checks for geometry and inventory conversion; run with unittest discovery."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import build_ghent
import ghent_canopy
import ghent_castle
import ghent_grb
import vlaanderen
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

    def test_kml_height_legend_colours(self):
        self.assertEqual(ghent_grb.kml_colour("64ffffff"), (255, 255, 255))
        self.assertEqual(ghent_grb.kml_colour("647aa0ff"), (255, 160, 122))
        self.assertEqual(ghent_grb.kml_colour("642222b2"), (178, 34, 34))
        with self.assertRaises(ValueError):
            ghent_grb.kml_colour("abc")

    def test_kml_stylemap_uses_the_normal_style(self):
        document = """
        <kml:Style id="style_tomato"><kml:PolyStyle><kml:color>644763ff</kml:color></kml:PolyStyle></kml:Style>
        <kml:Style id="style_blue"><kml:PolyStyle><kml:color>64ff0000</kml:color></kml:PolyStyle></kml:Style>
        <kml:StyleMap id="stylemap_tomato">
          <Pair><key>normal</key><styleUrl>#style_tomato</styleUrl></Pair>
          <Pair><key>highlight</key><styleUrl>#style_blue</styleUrl></Pair>
        </kml:StyleMap>
        """
        styles = ghent_grb.parse_styles(document)
        self.assertEqual(styles["stylemap_tomato"], (255, 99, 71))
        self.assertEqual(styles["style_blue"], (0, 0, 255))

    def test_canopy_keeps_a_green_crown_and_drops_a_car(self):
        dsm = np.full((20, 20), 8.0)
        dtm = np.full((20, 20), 6.0)
        dsm[4:7, 4:7] = 15.0  # tree crown 9 m above the ground
        dsm[14:17, 14:17] = 7.5  # parked car 1.5 m above the ground
        rgb = np.full((20, 20, 3), 100, np.uint8)
        rgb[..., 1] = 150
        building = np.zeros((20, 20), bool)
        mask = ghent_canopy.canopy_mask(dsm, dtm, building, rgb, np.array([[5, 5]]), min_area=1)
        self.assertTrue(mask[5, 5])
        self.assertFalse(mask[15, 15])
        building[4:7, 4:7] = True
        self.assertFalse(ghent_canopy.canopy_mask(dsm, dtm, building, rgb, None, min_area=1)[5, 5])

    def test_palette_quantization_keeps_materials_and_cap(self):
        rgb = np.random.default_rng(7).integers(0, 256, (30, 3), dtype=np.uint8)
        material = np.full(30, 12, np.uint8)
        base = [[0, 0, 0, 0], [158, 153, 140, 0]]
        entries, ids = build_ghent.palette_ids(rgb, material, base, cap=48)
        self.assertLessEqual(len(entries), 48)
        self.assertEqual(entries[:2], base)
        self.assertTrue(np.all(ids >= len(base)))
        self.assertTrue(all(entries[int(i)][3] == 12 for i in ids))

    def test_wcs_multipart_payload_and_rejection(self):
        body = (b'--wcs\r\nContent-Type: application/gml+xml\r\n\r\n<gml/>\r\n'
                b'--wcs\r\nContent-Type: image/tiff\r\n\r\nII*\x00payload\r\n--wcs--')
        response = SimpleNamespace(headers={"content-type": 'multipart/related; boundary="wcs"'}, content=body)
        self.assertEqual(vlaanderen.tiff_payload(response), b"II*\x00payload")
        plain = SimpleNamespace(headers={"content-type": "image/tiff"}, content=b"II*\x00plain")
        self.assertEqual(vlaanderen.tiff_payload(plain), b"II*\x00plain")
        with self.assertRaises(ValueError):
            vlaanderen.tiff_payload(SimpleNamespace(headers={"content-type": "text/xml"}, content=b"<html>"))

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

    def test_castle_lands_on_its_place_and_stands_above_the_ground(self):
        if not ghent_castle.MODEL.exists():
            self.skipTest("Gravensteen STL not present")
        ground = np.full((2000, 2000), 7.2)
        placed = ghent_castle.place(2000, 2000, 104000, 195000, ground)
        zone, building, roof = placed
        ys, xs = np.nonzero(building)
        self.assertAlmostEqual(xs.mean(), 573, delta=15)  # easting 104573
        self.assertAlmostEqual(ys.mean(), 694, delta=15)  # northing 194306
        self.assertTrue(np.all(zone[building]))
        self.assertTrue(np.all(roof[building] > 8.7))
        self.assertGreater(np.nanmax(roof), 30)
        self.assertIsNone(ghent_castle.place(100, 100, 0, 100, np.zeros((100, 100))))


if __name__ == "__main__":
    unittest.main()
