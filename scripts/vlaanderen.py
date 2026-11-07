"""Fetch Digitaal Vlaanderen rasters for the Ghent voxel map.

Two public OGC WCS services supply the data:

* ``https://geo.api.vlaanderen.be/dhmv/wcs`` -- DHMV II DSM and DTM, 1 m, EPSG:31370.
* ``https://geo.api.vlaanderen.be/oi-omz/wcs`` -- summer orthophoto mosaic (2021), RGB.

The DHMV service answers v2.0.1 as ``multipart/related`` with the GeoTIFF in a
second part; the ortho service answers v1.0.0 with a plain GeoTIFF. Both are
cached under ``data/raw/ghent`` so rebuilds need no network. Bounds are the same
2 x 2 km Lambert 72 rectangle the voxel builder uses.

Run ``python scripts/vlaanderen.py`` to refresh the cache, or import
``ensure_dhm`` / ``ensure_ortho`` from the builder.
"""
from __future__ import annotations

import argparse
import time
import os
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import rasterio
import requests

import ghent_area
from ghent_area import BLOCK, D, EAST, NORTH, SOUTH, W, WEST

ROOT = ghent_area.ROOT
CACHE = ghent_area.RAW
DSM_COVERAGE = "DHMVII_DSM_1m"
DTM_COVERAGE = "DHMVII_DTM_1m"
ORTHO_COVERAGE = "OI.OrthoimageCoverage.OMZ.RGB"
DHMV_URL = "https://geo.api.vlaanderen.be/dhmv/wcs"
ORTHO_URL = "https://geo.api.vlaanderen.be/oi-omz/wcs"
USER_AGENT = "ghent-voxel/0.2 (+https://github.com/HansF/voxelgent)"

# The DHMV gateway rejects an ``http://`` CRS URI in the query string (403 from
# the Azure application gateway) and the subsetting CRS is the native one, so
# requests omit it entirely.
DHMV_PARAMS = {
    "SERVICE": "WCS",
    "VERSION": "2.0.1",
    "REQUEST": "GetCoverage",
    "FORMAT": "image/tiff",
}
ORTHO_PARAMS = {
    "SERVICE": "WCS",
    "VERSION": "1.0.0",
    "REQUEST": "GetCoverage",
    "COVERAGE": ORTHO_COVERAGE,
    "CRS": "EPSG:31370",
    "RESX": 1,
    "RESY": 1,
    "FORMAT": "geoTIFF",
}


def _cache_path(coverage: str, x: int = WEST, y: int = SOUTH) -> Path:
    return CACHE / f"{coverage}_{x}_{y}.tif"


def _get(url: str, params: dict, timeout: int = 600) -> requests.Response:
    response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


def tiff_payload(response: requests.Response) -> bytes:
    """Return the GeoTIFF bytes, unpacking a ``multipart/related`` WCS response.

    MapServer on the DHMV service wraps the coverage in a multipart body whose
    first part is GML metadata. The payload keeps the TIFF even when the server
    labels the whole response ``multipart``.
    """
    content_type = response.headers.get("content-type", "")
    body = response.content
    if "multipart" not in content_type:
        if body[:4] not in (b"II*\x00", b"MM\x00*"):
            raise ValueError(f"Expected a GeoTIFF, got {content_type}: {body[:160]!r}")
        return body
    boundary = None
    for token in content_type.split(";")[1:]:
        name, _, value = token.strip().partition("=")
        if name.lower() == "boundary":
            boundary = value.strip('"')
    if not boundary:
        raise ValueError(f"Multipart response without a boundary: {content_type}")
    for part in body.split(b"--" + boundary.encode()):
        head, _, payload = part.partition(b"\r\n\r\n")
        if b"\n\n" in head and not payload:
            head, _, payload = part.partition(b"\n\n")
        if b"image/tiff" in head.lower():
            return payload.rstrip(b"\r\n")
    raise ValueError("Multipart WCS response has no image/tiff part")


def _checked_write(payload: bytes, target: Path, bounds_expected: tuple) -> Path:
    """Write only a raster that opens with the expected grid, never a partial file."""
    with rasterio.MemoryFile(payload) as memory, memory.open() as dataset:
        bounds = dataset.bounds
        if dataset.res != (1.0, 1.0) or dataset.crs is None:
            raise ValueError(f"Unexpected grid in {target.name}: {dataset.res}, {dataset.crs}")
        if (round(bounds.left), round(bounds.bottom), round(bounds.right), round(bounds.top)) != bounds_expected:
            raise ValueError(f"Unexpected bounds in {target.name}: {bounds}")
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


def _fetch(url: str, params: dict, target: Path, bounds: tuple, label: str) -> None:
    for attempt in range(4):
        try:
            print("fetching", label, flush=True)
            _checked_write(tiff_payload(_get(url, params)), target, bounds)
            return
        except (requests.RequestException, ValueError) as error:
            print("  retry", attempt + 1, error, flush=True)
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def ensure_dhm(cache: Path = CACHE, refresh: bool = False) -> list[tuple[Path, Path, int, int]]:
    """Download (or reuse) the 1 m DSM and DTM of every 2 km block: ``(dsm, dtm, x, y)``."""
    cache.mkdir(parents=True, exist_ok=True)
    result = []
    for x, y in ghent_area.blocks():
        bounds = (x, y, x + BLOCK, y + BLOCK)
        paths = []
        for coverage in (DSM_COVERAGE, DTM_COVERAGE):
            target = _cache_path(coverage, x, y)
            if refresh or not target.exists():
                params = dict(DHMV_PARAMS, COVERAGEID=coverage,
                              SUBSET=[f"X({bounds[0]},{bounds[2]})", f"Y({bounds[1]},{bounds[3]})"])
                _fetch(DHMV_URL, params, target, bounds, f"{coverage} {x},{y}")
            paths.append(target)
        result.append((paths[0], paths[1], x, y))
    return result


def ensure_ortho(cache: Path = CACHE, refresh: bool = False) -> list[tuple[Path, int, int]]:
    """Download (or reuse) the 1 m summer orthophoto of every 2 km block: ``(path, x, y)``."""
    cache.mkdir(parents=True, exist_ok=True)
    result = []
    for x, y in ghent_area.blocks():
        bounds = (x, y, x + BLOCK, y + BLOCK)
        target = _cache_path(ORTHO_COVERAGE, x, y)
        if refresh or not target.exists():
            params = dict(ORTHO_PARAMS, BBOX=",".join(map(str, bounds)))
            _fetch(ORTHO_URL, params, target, bounds, f"{ORTHO_COVERAGE} {x},{y}")
        result.append((target, x, y))
    return result


def read_grid(path: Path, width: int = BLOCK, depth: int = BLOCK) -> np.ndarray:
    """Read a cached block as ``(bands, depth, width)`` oriented row 0 = north."""
    with rasterio.open(path) as dataset:
        if (dataset.width, dataset.height) != (width, depth):
            raise ValueError(f"{path.name} is {dataset.width}x{dataset.height}, expected {width}x{depth}")
        return dataset.read()


def read_rgb(path: Path, width: int = BLOCK, depth: int = BLOCK) -> np.ndarray:
    """Read a cached ortho block as ``(depth, width, 3)`` uint8, row 0 = north."""
    return read_grid(path, width, depth).transpose(1, 2, 0)[..., :3]


def _place(out: np.ndarray, block: np.ndarray, x: int, y: int) -> None:
    r0, c0 = NORTH - (y + BLOCK), x - WEST
    out[r0:r0 + BLOCK, c0:c0 + BLOCK] = block


def read_surfaces() -> tuple[np.ndarray, np.ndarray]:
    """DSM and DTM mosaics for the whole area, ``(D, W)`` float32, row 0 = north."""
    dsm, dtm = (np.full((D, W), -9999, np.float32) for _ in range(2))
    for dsm_path, dtm_path, x, y in ensure_dhm():
        _place(dsm, read_grid(dsm_path)[0], x, y)
        _place(dtm, read_grid(dtm_path)[0], x, y)
    return dsm, dtm


def read_ortho() -> np.ndarray:
    """Summer orthophoto mosaic, ``(D, W, 3)`` uint8, row 0 = north."""
    out = np.zeros((D, W, 3), np.uint8)
    for path, x, y in ensure_ortho():
        _place(out, read_rgb(path), x, y)
    return out


# Local DHMV II download tiles some users keep beside the cache: k13/k14/k21/k22.
# ``GeoTIFF/DHMVIIDTMRAS1m_k22.tif`` alone covers easting 98-130 km, northing
# 178-198 km, i.e. this whole map. They are deflated at ~1.2 GB, so the WCS
# window stays the default; this helper exists for offline use.
def local_dtm(cache: Path = CACHE) -> Path | None:
    """Return an extracted local DTM GeoTIFF if one is present beside the zips."""
    direct = sorted(cache.glob("DHMVIIDTMRAS1m_k*_LB72.tif"))
    return direct[0] if direct else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="re-download cached rasters")
    args = parser.parse_args()
    for dsm, dtm, _x, _y in ensure_dhm(refresh=args.refresh):
        print(dsm, dtm)
    for path, _x, _y in ensure_ortho(refresh=args.refresh):
        print(path)


if __name__ == "__main__":
    main()
