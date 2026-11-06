"""Build a small, key-free Ghent timetable animation from a public GTFS ZIP.

python scripts/build_delijn_schedule.py [--download]
Dependencies: numpy, pyproj, shapely (same as the city builder).
Only route geometry and scheduled times are used. No real vehicle locations,
delays, cancellations or vehicle identifiers are inferred from this dataset.
"""
import argparse
import csv
import gzip
import hashlib
import io
import itertools
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, box

from delijn_routes import extract_routes

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://gtfs.flatturtle.cloud/delijn/_latest/delijn-gtfs.zip"
RAW = ROOT / "data/raw/ghent/delijn-gtfs.zip"
OUT = ROOT / "web/public/ghent/delijn-schedule.json.gz"
PROJECT = Transformer.from_crs(4326, 31370, always_xy=True)
WEST, NORTH, W, D = 104000, 195000, 2000, 2000


def rows(archive, name):
    if name not in archive.namelist():
        return
    with archive.open(name) as source:
        yield from csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig", newline=""))


def seconds(value):
    if not value:
        return None
    h, m, s = map(int, value.split(":"))
    if not (0 <= h <= 72 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"Invalid GTFS time {value}")
    return h * 3600 + m * 60 + s


def shape_chunks(points, bounds):
    """Keep contiguous source segments near the map; never join separated visits."""
    area = box(*bounds)
    chunks, current = [], []
    for a, b in zip(points[:-1], points[1:]):
        if LineString([a[:2], b[:2]]).intersects(area):
            if not current:
                current.append(a)
            current.append(b)
        elif current:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return [[[round(float(v), 2) for v in p] for p in chunk] for chunk in chunks]


def stop_distances(points, stop_xy):
    """Snap ordered stops to the route, preserving distance along loops."""
    starts, ends = points[:-1, :2], points[1:, :2]
    vectors = ends - starts
    lengths2 = (vectors * vectors).sum(axis=1)
    distances = []
    previous = 0.0
    for xy in stop_xy:
        weights = np.clip(((xy - starts) * vectors).sum(axis=1) / np.maximum(lengths2, 1e-10), 0, 1)
        along = points[:-1, 2] + weights * (points[1:, 2] - points[:-1, 2])
        errors = np.linalg.norm(starts + weights[:, None] * vectors - xy, axis=1)
        eligible = along >= previous - 0.5
        if not eligible.any():
            return None
        best = float(errors[eligible].min())
        if best > 150:  # Wrong/incompatible shape: don't invent a path through the city.
            return None
        candidates = np.flatnonzero(eligible & (errors <= best + 1.0))
        i = candidates[np.argmin(along[candidates])]
        previous = max(previous, float(along[i]))
        distances.append(round(previous, 2))
    return distances


def build(path):
    routes = extract_routes(path)
    with ZipFile(path) as archive:
        if next(rows(archive, "frequencies.txt"), None) is not None:
            raise ValueError("Frequency-based trips require a separate importer; refusing to treat templates as fixed departures")
        feed = next(rows(archive, "feed_info.txt"), {})
        agencies = list(rows(archive, "agency.txt"))
        if any(agency.get("agency_timezone") != "Europe/Brussels" for agency in agencies):
            raise ValueError("This importer expects De Lijn's Europe/Brussels timezone")
        full_shapes, chunks = {}, {}
        seen = set()
        print("Selecting Ghent route shapes…", flush=True)
        for identifier, group in itertools.groupby(rows(archive, "shapes.txt"), key=lambda row: row["shape_id"]):
            if identifier in seen:
                raise ValueError("shapes.txt must group points by shape_id")
            seen.add(identifier)
            records = sorted(group, key=lambda row: int(row["shape_pt_sequence"]))
            if len(records) < 2:
                continue
            lon = np.array([float(row["shape_pt_lon"]) for row in records])
            lat = np.array([float(row["shape_pt_lat"]) for row in records])
            if lon.max() < 3.70 or lon.min() > 3.76 or lat.max() < 51.035 or lat.min() > 51.075:
                continue
            east, north = PROJECT.transform(lon, lat)
            xy = np.stack([east - WEST, NORTH - north], axis=1)
            if not LineString(xy).intersects(box(0, 0, W, D)):
                continue
            cumulative = np.r_[0, np.linalg.norm(np.diff(xy, axis=0), axis=1).cumsum()]
            points = np.c_[xy, cumulative]
            full_shapes[identifier] = points
            chunks[identifier] = shape_chunks(points, (-80, -80, W + 80, D + 80))
        print(f"Selected {len(full_shapes)} shapes; selecting trips…", flush=True)
        trips = {row["trip_id"]: row for row in rows(archive, "trips.txt") if row.get("shape_id") in full_shapes}
        stops = {}
        for row in rows(archive, "stops.txt"):
            east, north = PROJECT.transform(float(row["stop_lon"]), float(row["stop_lat"]))
            stops[row["stop_id"]] = np.array([east - WEST, NORTH - north])
        services = {}
        for row in rows(archive, "calendar.txt"):
            services[row["service_id"]] = dict(start=row["start_date"], end=row["end_date"],
                weekdays=[int(row[day]) for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")], added=[], removed=[])
        for row in rows(archive, "calendar_dates.txt"):
            service = services.setdefault(row["service_id"], dict(start=None, end=None, weekdays=[], added=[], removed=[]))
            service["added" if row["exception_type"] == "1" else "removed"].append(row["date"])
        times = defaultdict(list)
        print(f"Reading stop times for {len(trips)} trips (streaming the regional file)…", flush=True)
        for row in rows(archive, "stop_times.txt"):
            identifier = row["trip_id"]
            if identifier not in trips:
                continue
            arrival, departure = seconds(row["arrival_time"]), seconds(row["departure_time"])
            if arrival is not None and departure is not None and row["stop_id"] in stops:
                times[identifier].append((int(row["stop_sequence"]), row["stop_id"], arrival, departure))
        print("Matching stops to route geometry…", flush=True)
        patterns, pattern_ids, result, skipped = {}, {}, [], 0
        for identifier, trip in trips.items():
            entries = sorted(times.get(identifier, []))
            if len(entries) < 2 or trip["service_id"] not in services:
                skipped += 1
                continue
            if any(a > d for _, _, a, d in entries) or any(a[3] > b[2] for a, b in zip(entries[:-1], entries[1:])):
                skipped += 1
                continue
            shape_id = trip["shape_id"]
            pattern_key = (shape_id, tuple(entry[1] for entry in entries))
            if pattern_key not in pattern_ids:
                snapped = stop_distances(full_shapes[shape_id], np.array([stops[entry[1]] for entry in entries]))
                pattern_ids[pattern_key] = None
                if snapped is not None:
                    # Keep timing anchors around every visit to the map, including bracketing stops.
                    low = min(chunk[0][2] for chunk in chunks[shape_id])
                    high = max(chunk[-1][2] for chunk in chunks[shape_id])
                    before = max(0, int(np.searchsorted(snapped, low, side="right")) - 1)
                    after = min(len(snapped), int(np.searchsorted(snapped, high, side="left")) + 1)
                    if after - before >= 2:
                        key = str(len(patterns))
                        patterns[key] = dict(shape=shape_id, distances=snapped[before:after], slice=[before, after])
                        pattern_ids[pattern_key] = key
            pattern = pattern_ids[pattern_key]
            if pattern is None:
                skipped += 1
                continue
            a, b = patterns[pattern]["slice"]
            anchors = [[entry[2], entry[3]] for entry in entries[a:b]]
            result.append(dict(id=hashlib.sha256(identifier.encode()).hexdigest()[:16], route=trip["route_id"],
                               service=trip["service_id"], pattern=pattern, headsign=trip.get("trip_headsign", ""), times=anchors))
        used_routes = {trip["route"] for trip in result}
        used_services = {trip["service"] for trip in result}
        used_shapes = {patterns[trip["pattern"]]["shape"] for trip in result}
        for pattern in patterns.values():
            pattern.pop("slice", None)
        output = dict(version=1, source=SOURCE, sourcePage="https://gtfs.flatturtle.cloud/delijn/", publisher="De Lijn via iRail / FlatTurtle",
                      generatedAt=datetime.now(timezone.utc).isoformat(), feedVersion=feed.get("feed_version"),
                      validFrom=feed.get("feed_start_date"), validUntil=feed.get("feed_end_date"), timezone="Europe/Brussels", W=W, D=D,
                      routes={key: routes[key] for key in used_routes}, services={key: services[key] for key in used_services},
                      shapes={key: chunks[key] for key in used_shapes}, patterns=patterns, trips=result,
                      maxTime=max((trip["times"][-1][1] for trip in result), default=86400),
                      limitations=["Scheduled estimates, not live GPS positions.", "Constant speed between timed stops, following published route shapes.",
                                   "Actual vehicle assignments, delays and cancellations are unknown."],
                      report=dict(trips=len(result), routes=len(used_routes), shapes=len(used_shapes), skippedTrips=skipped))
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--zip", type=Path, default=RAW)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    if args.download or not args.zip.exists():
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        temp = args.zip.with_suffix(".download")
        with urlopen(Request(SOURCE, headers={"User-Agent": "ghent-voxel/0.1"}), timeout=180) as response, temp.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        with ZipFile(temp) as archive:
            archive.getinfo("trips.txt")
        temp.replace(args.zip)
    output = build(args.zip)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    with gzip.GzipFile(temp, "wb", compresslevel=9, mtime=0) as file:
        file.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())
    temp.replace(args.output)
    print(json.dumps(output["report"]), flush=True)
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1048576:.2f} MB gzip)", flush=True)


if __name__ == "__main__":
    main()
