"""Extract optional line names and vehicle types from a De Lijn GTFS Static v3 ZIP.

Usage: python3 scripts/delijn_routes.py /path/to/gtfs_transit.zip
Uses a local ZIP; credentials are not required by this script.
"""
import argparse
import csv
import io
import json
from pathlib import Path
from zipfile import ZipFile


def extract_routes(path):
    with ZipFile(path) as archive:
        with archive.open("routes.txt") as source:
            rows = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig", newline=""))
            routes = {}
            for row in rows:
                code = int(row["route_type"])
                kind = "tram" if code == 0 or 900 <= code <= 906 else "bus" if code == 3 or 700 <= code <= 716 else "vehicle"
                routes[row["route_id"]] = {"name": row.get("route_short_name") or row.get("route_long_name") or row["route_id"], "kind": kind}
            return routes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/ghent/delijn-routes.json")
    args = parser.parse_args()
    result = extract_routes(args.zip)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"Wrote {len(result)} routes to {args.output}")
