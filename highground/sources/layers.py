"""Static DataSF layers: 100-year storm flood risk zone, storm + 24" sea level rise, neighborhoods.

Note: the SFPUC "100-Year Storm Flood Elevations" layer (wppz-u2hi) publishes water-surface elevation in
SF City Datum, not depth above ground, so HighGround does not claim flood depths.
"""

import httpx

from highground.http import get_json

LAYERS = {
    "100yr": {
        "dataset": "jzu3-4yxp",
        "name": "100-Year Storm Flood Risk Zone (SFPUC, July 2022)",
        "url": "https://www.sfpuc.gov/learning/emergency-preparedness/flood-maps",
    },
    "slr24": {
        "dataset": "esku-ejgv",
        "name": '100-Year Storm + 24" Sea Level Rise (BCDC Adapting to Rising Tides)',
        "url": "https://data.sfgov.org/d/esku-ejgv",
    },
}
NEIGHBORHOODS = "j2bu-swwd"


def geojson(http: httpx.Client, dataset: str) -> dict:
    return get_json(http, f"https://data.sf.gov/resource/{dataset}.geojson", params={"$limit": 5000}, timeout=180)


def zone_docs(layer: str, fc: dict) -> list[dict]:
    """Pure: split every MultiPolygon into one doc per polygon (smaller shapes = faster enrich and tiles)."""
    meta = LAYERS[layer]
    docs = []
    for fi, feat in enumerate(fc.get("features", [])):
        geom = feat.get("geometry") or {}
        polys = geom.get("coordinates", []) if geom.get("type") == "MultiPolygon" else [geom.get("coordinates", [])]
        props = feat.get("properties") or {}
        for pi, poly in enumerate(polys):
            if not poly:
                continue
            docs.append(
                {
                    "_id": f"{layer}:{fi}:{pi}",
                    "layer": layer,
                    "name": meta["name"],
                    "dataset_id": meta["dataset"],
                    "source_url": meta["url"],
                    "class": props.get("gridcode"),
                    "geometry": {"type": "Polygon", "coordinates": poly},
                }
            )
    return docs


def neighborhood_docs(fc: dict) -> list[dict]:
    docs = []
    for i, feat in enumerate(fc.get("features", [])):
        docs.append(
            {
                "_id": f"nhood:{i}",
                "layer": "neighborhood",
                "name": (feat.get("properties") or {}).get("nhood"),
                "dataset_id": NEIGHBORHOODS,
                "geometry": feat["geometry"],
            }
        )
    return docs
