"""Tree, shrub and hedge canopy from the DHMV II surface model.

The 1 m DSM minus DTM gives the height of everything above the bare earth:
roofs, walls, vehicles, street furniture and vegetation. Building footprints
remove the roofs; the summer orthophoto removes what is not green (cars, vans,
containers, cranes). A tree that the city inventory knows about is kept even
where its crown is too sparse to read as green at 1 m.

The result is a boolean canopy mask; the builder takes each cell's height from
the DSM, so crowns keep the real outline instead of a schematic sphere.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

# Height above the bare-earth DTM that counts as vegetation rather than a car,
# a low wall or street furniture.
MIN_HEIGHT_M = 2.5
# Taller than any tree in the map; keeps construction cranes and masts out.
MAX_HEIGHT_M = 45.0
# Crown fragments smaller than this are ortho noise or single shrubs that would
# add more tile bytes than visible shape.
MIN_AREA_CELLS = 4
# A cell this close to an inventoried tree is accepted even when the 2021 ortho
# reads brown/grey there (a sparse or recently pruned crown).
INVENTORY_REACH_M = 6


def greenness(rgb: np.ndarray) -> np.ndarray:
    """Excess green (2G - R - B) as int16, the usual vegetation index for RGB."""
    r, g, b = (rgb[..., i].astype(np.int16) for i in range(3))
    return 2 * g - r - b


def inventory_distance(points: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Euclidean distance in metres from every cell to the nearest point."""
    seeds = np.ones(shape, bool)
    for x, y in points:
        if 0 <= y < shape[0] and 0 <= x < shape[1]:
            seeds[int(y), int(x)] = False
    return ndimage.distance_transform_edt(seeds)


def canopy_mask(dsm: np.ndarray, dtm: np.ndarray, building: np.ndarray, rgb: np.ndarray,
                tree_points: np.ndarray | None = None, min_height: float = MIN_HEIGHT_M,
                max_height: float = MAX_HEIGHT_M, min_area: int = MIN_AREA_CELLS) -> np.ndarray:
    """Vegetation above 1 m, outside buildings, for the map grid.

    ``dsm``/``dtm`` are absolute metres (row 0 = north), ``building`` is the
    rasterized roof mask, ``rgb`` the summer orthophoto, ``tree_points`` an
    ``(n, 2)`` array of inventoried tree columns (x, y).
    """
    height = dsm - dtm
    # One cell of the roof edge is wall, not hedge: the DTM is bare earth under
    # the overhang, so DSM-DTM reads the full facade there.
    outside = ~ndimage.binary_dilation(building, iterations=1)
    green = greenness(rgb) > 8
    selected = (height > min_height) & (height < max_height) & outside & green
    if tree_points is not None and len(tree_points):
        near = inventory_distance(np.asarray(tree_points), height.shape) <= INVENTORY_REACH_M
        selected |= (height > min_height) & (height < max_height) & outside & near
    # A 2 x 2 opening drops isolated wall/furniture cells while keeping 1 m
    # crown detail; the area filter then drops the remaining specks.
    selected = ndimage.binary_opening(selected, structure=np.ones((2, 2), bool))
    labels, count = ndimage.label(selected)
    if count:
        sizes = np.bincount(labels.ravel())
        keep = sizes >= min_area
        keep[0] = False
        selected = keep[labels]
    return selected
