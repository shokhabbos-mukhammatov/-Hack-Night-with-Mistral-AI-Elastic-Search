"""Geocoding from the city's own records in Elasticsearch (intersections + EAS addresses), Census as fallback."""

import hashlib
from datetime import UTC, datetime

from elasticsearch import NotFoundError

from highground import http as http_mod
from highground.es import ES
from highground.geo import in_sf
from highground.sources.places import normalize_query, street_core

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def q_places(q: str) -> dict:
    """Pure: intersections match on alternates ("17TH & FOLSOM"), addresses on bool-prefix with fuzziness."""
    norm = normalize_query(q)
    if " & " in norm:
        a, b = [street_core(s.strip()) for s in norm.split(" & ", 1)]
        return {
            "size": 3,
            "query": {
                "bool": {
                    "filter": [{"term": {"kind": "intersection"}}],
                    "must": [
                        {"match": {"alt": {"query": f"{a} & {b}", "operator": "and", "fuzziness": "AUTO"}}},
                    ],
                }
            },
        }
    return {
        "size": 3,
        "query": {
            "bool": {
                "filter": [{"term": {"kind": "address"}}],
                "should": [
                    {"match_phrase_prefix": {"name": {"query": norm, "boost": 3}}},
                    {"multi_match": {"query": norm, "type": "bool_prefix", "fields": ["name", "name._2gram", "name._3gram"], "fuzziness": "AUTO", "operator": "and"}},
                    {"match": {"name": {"query": norm, "fuzziness": "AUTO", "operator": "and"}}},
                ],
                "minimum_should_match": 1,
            }
        },
    }


def geocode(es: ES, query: str) -> dict | None:
    q = (query or "").strip()
    if not q:
        return None
    key = hashlib.sha1(normalize_query(q).encode()).hexdigest()
    try:
        return es.client.get(index=es.ix("geocode-cache"), id=key)["_source"]
    except NotFoundError:
        pass
    hit = None
    try:
        hits = es.search("places", **q_places(q))["hits"]["hits"]
        if hits:
            s = hits[0]["_source"]
            hit = {"lat": s["location"]["lat"], "lon": s["location"]["lon"], "label": s["name"], "kind": s["kind"], "source": "DataSF EAS/intersections"}
    except Exception:  # noqa: BLE001
        hit = None
    if hit is None:
        hit = census(q)
    if hit:
        es.client.index(index=es.ix("geocode-cache"), id=key, document={**hit, "query": q, "created_at": datetime.now(UTC).isoformat()})
    return hit


def census(q: str) -> dict | None:
    try:
        with http_mod.client(timeout=15) as http:
            res = http_mod.get_json(
                http, CENSUS, params={"address": f"{q}, San Francisco, CA", "benchmark": "Public_AR_Current", "format": "json"}, retries=1
            )
        matches = res.get("result", {}).get("addressMatches", [])
        if not matches:
            return None
        m = matches[0]
        lat, lon = m["coordinates"]["y"], m["coordinates"]["x"]
        if not in_sf(lat, lon):
            return None
        return {"lat": lat, "lon": lon, "label": m["matchedAddress"], "kind": "address", "source": "US Census geocoder"}
    except Exception:  # noqa: BLE001
        return None


def reverse(es: ES, lat: float, lon: float) -> str | None:
    try:
        hits = es.search(
            "places",
            size=1,
            query={"bool": {"filter": [{"geo_distance": {"distance": "150m", "location": {"lat": lat, "lon": lon}}}]}},
            sort=[{"_geo_distance": {"location": {"lat": lat, "lon": lon}, "order": "asc", "unit": "m"}}],
        )["hits"]["hits"]
        return hits[0]["_source"]["name"] if hits else None
    except Exception:  # noqa: BLE001
        return None
