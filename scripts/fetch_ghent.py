"""Cache central Ghent's converted city tiles and OSM surface geometry.

Run with Python 3; no dependencies. STL coordinates are EPSG:31370 metres.
The default region covers easting 104000–106000, northing 193000–195000.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://storage.googleapis.com/fly-over-ghent/stl/"


def fetch(url, target):
    if target.exists():
        print("cached", target.name, flush=True)
        return
    print("fetching", target.name, flush=True)
    request = Request(url, headers={"User-Agent": "ghent-voxel/0.1"})
    with urlopen(request, timeout=180) as response:
        payload = response.read()
    if target.suffix == ".json":
        json.loads(payload)
    elif len(payload) < 84 or 84 + int.from_bytes(payload[80:84], "little") * 50 != len(payload):
        raise ValueError(f"Invalid binary STL: {url}")
    temp = target.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm-only", action="store_true")
    args = parser.parse_args()
    cache = ROOT / "data/raw/ghent"
    cache.mkdir(parents=True, exist_ok=True)
    if not args.osm_only:
        for x in (104000, 105000):
            for y in (193000, 194000):
                for kind, version in (("Geb", "2"), ("Trn", "0")):
                    name = f"{kind}_{x}_{y}_10_{version}_N_2013.stl"
                    fetch(BASE + name, cache / name)
    query = ('[out:json][timeout:90];('
             'way(51.044,3.711,51.065,3.744);'
             'relation["type"="multipolygon"](51.044,3.711,51.065,3.744);'
             ');out geom;')
    failures = []
    for endpoint in ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"):
        try:
            fetch(endpoint + "?" + urlencode({"data": query}), cache / "osm.json")
            break
        except Exception as error:
            failures.append(str(error))
    else:
        raise RuntimeError("OSM download failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
