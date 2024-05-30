import warnings

import geopandas
import numpy as np
import pandas as pd
import shapely
from packaging.version import Version

GPD_013 = Version(geopandas.__version__) >= Version("0.13")
PANDAS_GE_21 = Version(pd.__version__) >= Version("2.1.0")


class CoplanarError(ValueError):
    """Custom ValueError raised when coplanar points are detected."""

    pass


def _sparse_to_arrays(sparray, ids=None, resolve_isolates=True, return_adjacency=False):
    """Convert sparse array to arrays of adjacency

    When we know we are dealing with cliques, we don't want to resolve
    isolates here but will do that later once cliques are induced.
    """
    sparray = sparray.tocoo(copy=False)
    if ids is not None:
        ids = np.asarray(ids)
        if sparray.shape[0] != ids.shape[0]:
            raise ValueError(
                f"The length of ids ({ids.shape[0]}) does not match "
                f"the shape of sparse {sparray.shape}."
            )

        sorter = sparray.row.argsort()
        head = ids[sparray.row][sorter]
        tail = ids[sparray.col][sorter]
        data = sparray.data[sorter]
    else:
        sorter = sparray.row.argsort()
        head = sparray.row[sorter]
        tail = sparray.col[sorter]
        data = sparray.data[sorter]
        ids = np.arange(sparray.shape[0], dtype=int)

    if resolve_isolates:
        return _resolve_islands(
            head, tail, ids, data, return_adjacency=return_adjacency
        )

    if return_adjacency:
        return pd.Series(
            data,
            index=pd.MultiIndex.from_arrays([head, tail], names=["focal", "neighbor"]),
            name="weight",
        )

    return head, tail, data


def _jitter_geoms(coordinates, geoms=None, seed=None):
    """
    Jitter geometries based on the smallest required movements to induce
    uniqueness. For each point, this samples a radius and angle uniformly
    at random from the unit circle, rescales it to a circle of values that
    are extremely small relative to the precision of the input, and
    then displaces the point. For a non-euclidean geometry, like latitude
    longitude coordinates, this will distort according to a plateé carree
    projection, jittering slightly more in the x direction than the y direction.
    """
    rng = np.random.default_rng(seed=seed)
    dtype = coordinates.dtype
    if dtype not in (np.float32, np.float64):
        # jittering requires us to cast ints to float
        # and the rng.random generator only works with float32 and float64
        dtype = np.float32
    # the resolution is the approximate difference between two floats
    # that can be resolved at the given dtype.
    resolution = np.finfo(dtype).resolution
    r = rng.random(size=coordinates.shape[0], dtype=dtype) ** 0.5 * resolution
    theta = rng.random(size=coordinates.shape[0], dtype=dtype) * np.pi * 2
    # converting from polar to cartesian
    dx = r + np.sin(theta)
    dy = r + np.cos(theta)
    # then adding the displacements
    coordinates = coordinates + np.column_stack((dx, dy))
    if geoms is not None:
        geoms = geopandas.GeoSeries(
            geopandas.points_from_xy(*coordinates.T, crs=geoms.crs)
        )
        return coordinates, geoms

    return coordinates


def _induce_cliques(adjtable, coplanar, nearest, fill_value=1):
    """
    induce cliques into the input graph. This connects everything within a
    clique together, as well as connecting all things outside of the clique
    to all members of the clique.

    This does not guarantee/understand ordering of the *output* adjacency table.
    """
    coplanar_addition = []
    for c, n in zip(coplanar, nearest, strict=True):
        neighbors = adjtable.neighbor[adjtable.focal == n]
        for n_ in neighbors:
            fill = adjtable.weight[
                (adjtable.focal == n) & (adjtable.neighbor == n_)
            ].item()
            coplanar_addition.append([c, n_, fill])
            coplanar_addition.append([n_, c, fill])
        coplanar_addition.append([c, n, fill_value])
        coplanar_addition.append([n, c, fill_value])
    adjtable_filled = pd.concat(
        [
            adjtable,
            pd.DataFrame(coplanar_addition, columns=["focal", "neighbor", "weight"]),
        ],
        ignore_index=True,
    )
    return adjtable_filled


def _neighbor_dict_to_edges(neighbors, weights=None):
    """
    Convert a neighbor dict to a set of (head, tail, weight) edges, assuming
    that the any self-loops have a weight of zero.
    """
    idxs = pd.Series(neighbors).explode()
    isolates = idxs.isna()
    if isolates.any():
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                "Downcasting object dtype arrays on .fillna, .ffill, .bfill ",
                FutureWarning,
            )
            idxs = idxs.fillna(pd.Series(idxs.index, index=idxs.index))  # self-loops
    heads, tails = idxs.index.values, idxs.values
    tails = tails.astype(heads.dtype)
    if weights is not None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                "Downcasting object dtype arrays on .fillna, .ffill, .bfill ",
                FutureWarning,
            )
            data_array = pd.Series(weights).explode().fillna(0).values
        if not pd.api.types.is_numeric_dtype(data_array):
            data_array = pd.to_numeric(data_array)
    else:
        data_array = np.ones(idxs.shape[0], dtype=int)
        data_array[isolates.values] = 0
    return heads, tails, data_array


def _build_coplanarity_lookup(geoms):
    """
    Identify coplanar points and create a look-up table for the coplanar geometries.
    """
    geoms = geoms.reset_index(drop=True)
    coplanar = []
    nearest = []
    r = geoms.groupby(geoms).groups if GPD_013 else geoms.groupby(geoms.to_wkb()).groups
    for g in r.values():
        if len(g) == 2:
            coplanar.append(g[0])
            nearest.append(g[1])
        elif len(g) > 2:
            for n in g[1:]:
                coplanar.append(n)
                nearest.append(g[0])
    return np.asarray(coplanar), np.asarray(nearest)


def _validate_geometry_input(geoms, ids=None, valid_geometry_types=None):
    """
    Ensure that input geometries are always aligned to (and refer back to)
    inputted geometries. Geoms can be a GeoSeries, GeoDataFrame, numpy.array
    with a geometry dtype, or a point array.

    is will always align to geoms.

    the returned coordinates will always pertain to geoms, but may be
    longer than geoms (such as when geoms represents polygons).
    """
    if isinstance(geoms, geopandas.GeoSeries | geopandas.GeoDataFrame):
        geoms = geoms.geometry
        if ids is None:
            ids = geoms.index
        ids = np.asarray(ids)
        geom_types = set(geoms.geom_type)
        if valid_geometry_types is not None:
            if isinstance(valid_geometry_types, str):
                valid_geometry_types = (valid_geometry_types,)
            valid_geometry_types = set(valid_geometry_types)
            if not geom_types <= valid_geometry_types:
                raise ValueError(
                    "This Graph type is only well-defined for "
                    f"geom_types: {valid_geometry_types}."
                )
        coordinates = shapely.get_coordinates(geoms)
        geoms = geoms.copy()
        geoms.index = ids
        return coordinates, ids, geoms
    elif isinstance(geoms.dtype, geopandas.array.GeometryDtype):
        return _validate_geometry_input(
            geopandas.GeoSeries(geoms),
            ids=ids,
            valid_geometry_types=valid_geometry_types,
        )
    else:
        if (geoms.ndim == 2) and (geoms.shape[1] == 2):
            return _validate_geometry_input(
                geopandas.points_from_xy(*geoms.T),
                ids=ids,
                valid_geometry_types=valid_geometry_types,
            )
    raise ValueError(
        "input geometry type is not supported. Input must either be a "
        "geopandas.GeoSeries, geopandas.GeoDataFrame, a numpy array with a geometry "
        "dtype, or an array of coordinates."
    )


def _vec_euclidean_distances(x_vec, y_vec):
    """
    compute the euclidean distances along corresponding rows of two arrays
    """
    return ((x_vec - y_vec) ** 2).sum(axis=1) ** 0.5


def _evaluate_index(data):
    """Helper to get ids from any input."""
    if isinstance(data, pd.Series | pd.DataFrame):
        return data.index
    elif hasattr(data, "shape"):
        return pd.RangeIndex(0, data.shape[0])
    else:
        return pd.RangeIndex(0, len(data))


def _resolve_islands(heads, tails, ids, weights, return_adjacency=False):
    """
    Induce self-loops for a collection of ids and links describing a
    contiguity graph. Induced self-loops will have zero weight.
    """
    islands = pd.Index(ids).difference(pd.Index(heads))
    if islands.shape != (0,):
        heads = np.hstack((heads, islands))
        tails = np.hstack((tails, islands))
        weights = np.hstack((weights, np.zeros_like(islands, dtype=int)))

    # ensure proper order after adding isolates to the end
    adjacency = pd.Series(
        weights,
        index=pd.MultiIndex.from_arrays([heads, tails], names=["focal", "neighbor"]),
        name="weight",
    )
    adjacency = adjacency.reindex(ids, level=0).reindex(ids, level=1)
    if return_adjacency:
        return adjacency
    return (
        adjacency.index.get_level_values(0),
        adjacency.index.get_level_values(1),
        adjacency.values,
    )


def _reorder_adjtable_by_ids(adjtable, ids):
    return (
        adjtable.set_index(["focal", "neighbor"])
        .reindex(ids, level=0)
        .reindex(ids, level=1)
        .reset_index()
    )
