"""Which part of Ghent the scripts work on (Lambert 72 metres, 1 voxel = 1 m).

GHENT_AREA=centre  2 x 2 km historic centre (the original map, no file suffix)
GHENT_AREA=ring    8 x 8 km inside the R4 ring (files get a ``_ring`` suffix)
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AREAS = {"centre": (104000, 193000, 106000, 195000), "ring": (101000, 190000, 109000, 198000)}
NAME = os.environ.get("GHENT_AREA", "centre")
if NAME not in AREAS:
    raise SystemExit(f"GHENT_AREA must be one of {sorted(AREAS)}, not {NAME!r}")
WEST, SOUTH, EAST, NORTH = AREAS[NAME]
W, D = EAST - WEST, NORTH - SOUTH
SUFFIX = "" if NAME == "centre" else f"_{NAME}"
BLOCK = 2000  # WCS requests and STL work happen in 2 km blocks
RAW = ROOT / "data/raw/ghent"
OSM = RAW / f"osm{SUFFIX}.json"
OSM_BRIDGES = RAW / f"osm_bridges{SUFFIX}.json"
TREES = ROOT / f"data/ghent/trees{SUFFIX}.json"
GRB_CACHE = ROOT / f"data/ghent/grb_colours{SUFFIX}.npz"


def blocks():
    """Lower-left corners of the 2 km blocks covering the area."""
    return [(x, y) for y in range(SOUTH, NORTH, BLOCK) for x in range(WEST, EAST, BLOCK)]


def stl_tiles():
    """Lower-left corners of the 1 km STL tiles covering the area."""
    return [(x, y) for x in range(WEST, EAST, 1000) for y in range(SOUTH, NORTH, 1000)]
