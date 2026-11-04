"""Cell profiles: slow-changing susceptibility per H3 cell, computed from ES as of a date (no look-ahead).

- storm days: citywide days with >= 100 flooding reports (Pacific calendar days), 5 years before as_of
- per cell: flooding reports on those days within the cell + its ring(1) neighbours
- zones: whether the cell polygon touches the 100-yr storm zone / the storm + 24" SLR area
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

import h3

from highground.es import ES
from highground.geo import H3_RES, cell_center, cell_polygon_geojson, ring
from highground.models import CellProfile, ZoneInfo

STORM_DAY_MIN = 100
WINDOW = timedelta(days=int(365.25 * 5))


def storm_days(es: ES, as_of: datetime, min_reports: int = STORM_DAY_MIN) -> list[str]:
    res = es.search(
        "hazard-reports",
        size=0,
        query={"bool": {"filter": [{"term": {"category": "flooding"}}, _window(as_of)]}},
        aggs={
            "d": {
                "date_histogram": {
                    "field": "requested_at",
                    "calendar_interval": "day",
                    "time_zone": "America/Los_Angeles",
                    "min_doc_count": min_reports,
                    "format": "yyyy-MM-dd",
                }
            }
        },
    )
    return [b["key_as_string"] for b in res["aggregations"]["d"]["buckets"]]


def _window(as_of: datetime) -> dict:
    return {"range": {"requested_at": {"gte": (as_of - WINDOW).isoformat(), "lt": as_of.isoformat()}}}


def _day_ranges(days: list[str]) -> list[dict]:
    return [
        {"range": {"requested_at": {"gte": f"{d}T00:00:00", "lte": f"{d}T23:59:59", "time_zone": "America/Los_Angeles"}}}
        for d in days
    ]


def cell_counts(es: ES, as_of: datetime, days: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    base = {"bool": {"filter": [{"term": {"category": "flooding"}}, _window(as_of)]}}
    res = es.search(
        "hazard-reports",
        size=0,
        query=base,
        aggs={
            "all": {"terms": {"field": "h3_9", "size": 20000}},
            "storm": {
                "filter": {"bool": {"should": _day_ranges(days), "minimum_should_match": 1}} if days else {"match_none": {}},
                "aggs": {"cells": {"terms": {"field": "h3_9", "size": 20000}}},
            },
        },
    )
    a = res["aggregations"]
    total = {b["key"]: b["doc_count"] for b in a["all"]["buckets"]}
    storm = {b["key"]: b["doc_count"] for b in a["storm"]["cells"]["buckets"]}
    return total, storm


def city_cells(es: ES) -> dict[str, str]:
    """Every res-9 cell whose centre lies in an SF analysis neighborhood -> neighborhood name."""
    res = es.search("flood-zones", size=100, query={"term": {"layer": "neighborhood"}}, _source=["name", "geometry"])
    out: dict[str, str] = {}
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        shape = h3.geo_to_h3shape(src["geometry"])
        for c in h3.h3shape_to_cells(shape, H3_RES):
            out[c] = src["name"]
    return out


def zone_flags(es: ES, cells: list[str]) -> dict[str, dict[str, bool]]:
    flags: dict[str, dict[str, bool]] = defaultdict(lambda: {"100yr": False, "slr24": False})
    for i in range(0, len(cells), 250):
        chunk = cells[i : i + 250]
        searches = []
        for c in chunk:
            for layer in ("100yr", "slr24"):
                searches.append({"index": es.ix("flood-zones")})
                searches.append(
                    {
                        "size": 0,
                        "terminate_after": 1,
                        "track_total_hits": 1,
                        "query": {
                            "bool": {
                                "filter": [
                                    {"term": {"layer": layer}},
                                    {"geo_shape": {"geometry": {"shape": cell_polygon_geojson(c), "relation": "intersects"}}},
                                ]
                            }
                        },
                    }
                )
        res = es.client.msearch(searches=searches)
        it = iter(res["responses"])
        for c in chunk:
            for layer in ("100yr", "slr24"):
                r = next(it)
                flags[c][layer] = r.get("hits", {}).get("total", {}).get("value", 0) > 0
    return flags


def compute_profiles(es: ES, as_of: datetime) -> tuple[dict[str, CellProfile], dict]:
    days = storm_days(es, as_of)
    total, storm = cell_counts(es, as_of, days)
    nh = city_cells(es)
    cells = sorted(set(nh) | set(total))
    flags = zone_flags(es, cells)
    profiles = {}
    for c in cells:
        neigh = ring(c, 1)
        f = flags[c]
        profiles[c] = CellProfile(
            cell=c,
            zone=ZoneInfo(in_100yr=f["100yr"], slr24=f["slr24"], shoreline=f["slr24"]),
            reports_5y=sum(total.get(n, 0) for n in neigh),
            storm_reports_5y=sum(storm.get(n, 0) for n in neigh),
            storm_days_5y=len(days),
        )
    meta = {"as_of": as_of.isoformat(), "storm_days": days, "cells": len(cells), "neighborhoods": nh}
    return profiles, meta


def build_profiles(es: ES, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(UTC)
    profiles, meta = compute_profiles(es, as_of)
    docs = []
    for c, p in profiles.items():
        lat, lon = cell_center(c)
        docs.append(
            {
                "_id": c,
                "cell": c,
                "h3_9": c,
                "in_100yr": p.zone.in_100yr,
                "slr24": p.zone.slr24,
                "shoreline": p.zone.shoreline,
                "reports_5y": p.reports_5y,
                "storm_reports_5y": p.storm_reports_5y,
                "storm_days_5y": p.storm_days_5y,
                "centroid": {"lat": lat, "lon": lon},
                "neighborhood": meta["neighborhoods"].get(c),
                "as_of": as_of.isoformat(),
            }
        )
    n = es.upsert_docs("cell-profiles", docs)
    return {
        "cells": n,
        "storm_days": meta["storm_days"],
        "in_100yr": sum(p.zone.in_100yr for p in profiles.values()),
        "slr24": sum(p.zone.slr24 for p in profiles.values()),
    }


def profile_from_doc(src: dict) -> CellProfile:
    return CellProfile(
        cell=src["cell"],
        zone=ZoneInfo(in_100yr=src.get("in_100yr", False), slr24=src.get("slr24", False), shoreline=src.get("shoreline", False)),
        reports_5y=src.get("reports_5y", 0),
        storm_reports_5y=src.get("storm_reports_5y", 0),
        storm_days_5y=src.get("storm_days_5y", 0),
    )
