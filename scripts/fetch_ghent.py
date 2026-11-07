"""Cache central Ghent's converted city tiles and OSM surface geometry.

Run with Python 3; no dependencies. STL coordinates are EPSG:31370 metres.
The region is chosen with GHENT_AREA (see ghent_area.py); the default is easting 104000–106000, northing 193000–195000.
"""
import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ghent_area
from pyproj import Transformer

ROOT = ghent_area.ROOT
BASE = "https://storage.googleapis.com/fly-over-ghent/stl/"


def fetch(url, target, optional=False):
    if target.exists():
        print("cached", target.name, flush=True)
        return True
    print("fetching", target.name, flush=True)
    request = Request(url, headers={"User-Agent": "ghent-voxel/0.1"})
    try:
        with urlopen(request, timeout=180) as response:
            payload = response.read()
    except HTTPError as error:
        if optional and error.code == 404:
            print("  no tile (404)", target.name, flush=True)
            return False
        raise
    if target.suffix == ".json":
        json.loads(payload)
    elif len(payload) < 84 or 84 + int.from_bytes(payload[80:84], "little") * 50 != len(payload):
        raise ValueError(f"Invalid binary STL: {url}")
    temp = target.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(target)
    return True


OSM_TAGS = ('way["highway"]', 'way["landuse"]', 'way["leisure"]', 'way["natural"]', 'way["waterway"]', 'way["water"]',
            'way["historic"]["name"]', 'way["tourism"]["name"]', 'way["amenity"]["name"]',
            'way["building"~"church|cathedral"]', 'relation["type"="multipolygon"]["landuse"]',
            'relation["type"="multipolygon"]["leisure"]', 'relation["type"="multipolygon"]["natural"]',
            'relation["type"="multipolygon"]["water"]', 'relation["type"="multipolygon"]["historic"]',
            'relation["type"="multipolygon"]["building"~"church|cathedral"]')


def osm_bridges():
    """Every bridge of the area (lines and man_made=bridge outlines), queried per 2 km block."""
    to_wgs = Transformer.from_crs(31370, 4326, always_xy=True)
    elements, stamp = {}, "unknown"
    for x, y in ghent_area.blocks():
        corners = [to_wgs.transform(x + dx, y + dy) for dx in (-100, ghent_area.BLOCK + 100) for dy in (-100, ghent_area.BLOCK + 100)]
        bbox = f"({min(c[1] for c in corners):.5f},{min(c[0] for c in corners):.5f},{max(c[1] for c in corners):.5f},{max(c[0] for c in corners):.5f})"
        query = ('[out:json][timeout:120];(way["bridge"]["bridge"!="no"]' + bbox + ';way["man_made"="bridge"]' + bbox
                 + ';relation["man_made"="bridge"]' + bbox + ';);out geom;')
        part = ghent_area.RAW / f"osm_bridges_part_{x}_{y}.json"
        for attempt in range(6):
            failures = []
            for endpoint in ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"):
                try:
                    if fetch(endpoint + "?" + urlencode({"data": query}), part):
                        break
                except Exception as error:
                    failures.append(str(error))
            else:
                print("  bridges retry", attempt + 1, failures, flush=True)
                time.sleep(20 * (attempt + 1))
                continue
            break
        else:
            raise RuntimeError("OSM bridge download failed: " + "; ".join(failures))
        data = json.loads(part.read_text())
        stamp = data.get("osm3s", {}).get("timestamp_osm_base", stamp)
        for item in data["elements"]:
            elements[(item["type"], item["id"])] = item
    ghent_area.OSM_BRIDGES.write_text(json.dumps({"osm3s": {"timestamp_osm_base": stamp}, "elements": list(elements.values())}, separators=(",", ":")))
    print(f"{len(elements)} bridge elements -> {ghent_area.OSM_BRIDGES.name}", flush=True)


def osm_area():
    """OSM surface geometry of a large area, queried per 2 km block with only the tags the builder uses."""
    to_wgs = Transformer.from_crs(31370, 4326, always_xy=True)
    elements, stamp = {}, "unknown"
    for x, y in ghent_area.blocks():
        corners = [to_wgs.transform(x + dx, y + dy) for dx in (-100, ghent_area.BLOCK + 100) for dy in (-100, ghent_area.BLOCK + 100)]
        south, north = min(c[1] for c in corners), max(c[1] for c in corners)
        west, east = min(c[0] for c in corners), max(c[0] for c in corners)
        bbox = f"({south:.5f},{west:.5f},{north:.5f},{east:.5f})"
        query = "[out:json][timeout:180];(" + ";".join(t + bbox for t in OSM_TAGS) + ";);out geom;"
        part = ghent_area.RAW / f"osm_part_{x}_{y}.json"
        for attempt in range(6):
            failures = []
            for endpoint in ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"):
                try:
                    if fetch(endpoint + "?" + urlencode({"data": query}), part):
                        break
                except Exception as error:  # network, 429, 504: try the other mirror, then wait
                    failures.append(str(error))
            else:
                print("  osm retry", attempt + 1, failures, flush=True)
                time.sleep(20 * (attempt + 1))
                continue
            break
        else:
            raise RuntimeError("OSM download failed: " + "; ".join(failures))
        data = json.loads(part.read_text())
        stamp = data.get("osm3s", {}).get("timestamp_osm_base", stamp)
        for item in data["elements"]:
            elements[(item["type"], item["id"])] = item
    merged = json.dumps({"osm3s": {"timestamp_osm_base": stamp}, "elements": list(elements.values())}, separators=(",", ":"))
    ghent_area.OSM.write_text(merged)
    print(f"{len(elements)} OSM elements -> {ghent_area.OSM.name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm-only", action="store_true")
    parser.add_argument("--bridges-only", action="store_true")
    args = parser.parse_args()
    cache = ghent_area.RAW
    cache.mkdir(parents=True, exist_ok=True)
    if args.bridges_only:
        osm_bridges()
        return
    if not args.osm_only:
        for x, y in ghent_area.stl_tiles():
            for kind, version in (("Geb", "2"), ("Trn", "0")):
                name = f"{kind}_{x}_{y}_10_{version}_N_2013.stl"
                fetch(BASE + name, cache / name, optional=True)
    if ghent_area.NAME != "centre":
        osm_area()
        osm_bridges()
        return
    query = ('[out:json][timeout:90];('
             'way(51.044,3.711,51.065,3.744);'
             'relation["type"="multipolygon"](51.044,3.711,51.065,3.744);'
             ');out geom;')
    failures = []
    for endpoint in ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"):
        try:
            fetch(endpoint + "?" + urlencode({"data": query}), ghent_area.OSM)
            break
        except Exception as error:
            failures.append(str(error))
    else:
        raise RuntimeError("OSM download failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
