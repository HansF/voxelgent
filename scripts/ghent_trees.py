"""Download and normalize Ghent's tree inventory, independently of Warsaw's grid.

Usage: python3 scripts/ghent_trees.py [--refresh] [--bbox WEST SOUTH EAST NORTH]
Output coordinates are WGS84; the Ghent voxel builder must project these into its grid.
Only the Python standard library is required. Source dimensions remain unconverted
where the publisher does not document units.
"""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = "https://data.stad.gent/api/explore/v2.1/catalog/datasets/locaties-bomen-gent"
SOURCE = "https://data.stad.gent/explore/dataset/locaties-bomen-gent/"


def download(url, path):
    request = Request(url, headers={"User-Agent": "ghent-voxel/0.1"})
    with urlopen(request, timeout=180) as response:
        payload = response.read()
    json.loads(payload)  # Never cache an HTML error page or incomplete JSON.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(path)


def height_range(value):
    """Interpret bounded metric ranges only; retain open-ended classes as raw data."""
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*m\.?\s*", str(value))
    if not match:
        return None
    low, high = (float(v.replace(",", ".")) for v in match.groups())
    return [low, high] if 0 <= low <= high <= 100 else None


def normalize(records, bbox=None):
    trees, seen = [], set()
    skipped = {"coordinates": 0, "outside_bbox": 0, "duplicate": 0}
    for record in records:
        point = record.get("geo_point_2d") or {}
        try:
            lon, lat = float(point["lon"]), float(point["lat"])
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("invalid coordinate")
        except (KeyError, TypeError, ValueError):
            skipped["coordinates"] += 1
            continue
        if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            skipped["outside_bbox"] += 1
            continue
        identifier = record.get("gis_id")
        if not identifier:
            fingerprint = json.dumps([lon, lat, record.get("sortiment")], ensure_ascii=False)
            identifier = "location:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
        identifier = f"gent:{identifier}"
        if identifier in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(identifier)
        heights = height_range(record.get("hoogte"))
        trees.append({
            "id": identifier, "src": "stad-gent", "lon": lon, "lat": lat,
            "species": record.get("sortiment"), "street": record.get("straatnaam"),
            "height_range_m": heights,
            "height_estimate_m": sum(heights) / 2 if heights else None,
            "height_class": record.get("hoogte"),
            "diameter_raw": record.get("diameter"),
            "trunk_circumference_raw": record.get("stamomtrek"),
            "planting_date": record.get("aanlegjaar"), "manager": record.get("beheerder"),
            "owner": record.get("eigenaar"), "protected": record.get("bescherm"),
        })
    return trees, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--output", type=Path, default=ROOT / "data/ghent/trees.json")
    args = parser.parse_args()
    if args.bbox and not (-180 <= args.bbox[0] < args.bbox[2] <= 180 and -90 <= args.bbox[1] < args.bbox[3] <= 90):
        parser.error("bbox must have ordered WGS84 bounds")
    cache = ROOT / "data/raw/ghent"
    for name, url in (("trees", API + "/exports/json"), ("trees-metadata", API)):
        path = cache / f"{name}.json"
        if args.refresh or not path.exists():
            download(url, path)
    records = json.loads((cache / "trees.json").read_text())
    metadata = json.loads((cache / "trees-metadata.json").read_text())["metas"]["default"]
    trees, skipped = normalize(records, args.bbox)
    result = {
        "source": SOURCE, "publisher": metadata.get("publisher"),
        "license": metadata.get("license"), "license_url": metadata.get("license_url"),
        "source_modified": metadata.get("modified"), "crs": "EPSG:4326",
        "bbox": args.bbox, "source_count": len(records), "count": len(trees), "skipped": skipped,
        "limitations": ["Inventory is incomplete, according to the publisher.",
                        "Height midpoints are estimates from source ranges.",
                        "Diameter and circumference units have not been verified.",
                        "Some trunk circumferences are street/species averages."],
        "trees": trees,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"Wrote {len(trees):,} trees to {args.output}; skipped {skipped}")


if __name__ == "__main__":
    main()
