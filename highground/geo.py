"""Geo helpers: H3 cells ("blocks"), distances, and the San Francisco bounding box."""

import math

import h3

H3_RES = 9  # ~0.1 km² per cell: roughly a city block or two
SF_BBOX = (-122.5155, 37.7080, -122.3549, 37.8120)  # lon_min, lat_min, lon_max, lat_max (land + near-shore)


def cell_of(lat: float, lon: float, res: int = H3_RES) -> str:
    return h3.latlng_to_cell(lat, lon, res)


def ring(cell: str, k: int = 1) -> list[str]:
    return list(h3.grid_disk(cell, k))


def cell_center(cell: str) -> tuple[float, float]:
    return h3.cell_to_latlng(cell)


def cell_polygon_geojson(cell: str) -> dict:
    """GeoJSON polygon (lon, lat order, closed ring) for an H3 cell."""
    boundary = h3.cell_to_boundary(cell)  # ((lat, lon), ...)
    coords = [[lon, lat] for lat, lon in boundary]
    coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_sf(lat: float, lon: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = SF_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


# Weather sample points across SF. Rain and wind are fetched per point; each cell uses the nearest point.
WEATHER_POINTS: dict[str, tuple[float, float]] = {
    "nw": (37.780, -122.480),  # Richmond / Golden Gate Park north
    "sw": (37.735, -122.480),  # Sunset / Parkside
    "c": (37.760, -122.435),  # Twin Peaks / Castro
    "ne": (37.790, -122.410),  # Downtown / North Beach
    "se": (37.735, -122.395),  # Bayview / Bernal
}


def nearest_weather_point(lat: float, lon: float) -> str:
    return min(WEATHER_POINTS, key=lambda k: haversine_m(lat, lon, *WEATHER_POINTS[k]))
