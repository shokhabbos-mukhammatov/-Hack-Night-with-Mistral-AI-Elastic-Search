"""ES query builders (pure, unit-tested) + thin executors. Every tool returns plain JSON-able data.

These are also the Mistral agent's tools: read-only by construction.
"""

from datetime import UTC, datetime, timedelta

from highground.es import ES
from highground.geo import cell_of, haversine_m
from highground.models import EventKind, NearbyEvent, NwsAlert, Tide, Weather
from highground.sources.hazards import parse_utc

FLOOD_KINDS = {"flooding": EventKind.flood_report, "catch_basin": EventKind.flood_report, "sewer_backup": EventKind.sewer_report}

# ---------------------------------------------------------------- pure query builders


def q_zone_at(lat: float, lon: float) -> dict:
    return {
        "size": 10,
        "_source": ["layer", "name", "source_url"],
        "query": {"geo_shape": {"geometry": {"shape": {"type": "point", "coordinates": [lon, lat]}, "relation": "intersects"}}},
    }


def q_nearby_reports(lat: float, lon: float, since: datetime, until: datetime, radius_m: int = 300, size: int = 50) -> dict:
    return {
        "size": size,
        "sort": [{"requested_at": "desc"}],
        "_source": ["case_id", "category", "subcategory", "requested_at", "location", "address", "status", "vision", "description", "media_url"],
        "query": {
            "bool": {
                "filter": [
                    {"geo_distance": {"distance": f"{radius_m}m", "location": {"lat": lat, "lon": lon}}},
                    {"range": {"requested_at": {"gte": since.isoformat(), "lte": until.isoformat()}}},
                    {"terms": {"category": ["flooding", "catch_basin", "sewer_backup", "tree", "street"]}},
                ]
            }
        },
    }


def q_nearby_points(index_time_field: str, lat: float, lon: float, since: datetime, until: datetime, radius_m: int) -> dict:
    return {
        "size": 30,
        "sort": [{index_time_field: "desc"}],
        "query": {
            "bool": {
                "filter": [
                    {"geo_distance": {"distance": f"{radius_m}m", "location": {"lat": lat, "lon": lon}}},
                    {"range": {index_time_field: {"gte": since.isoformat(), "lte": until.isoformat()}}},
                ]
            }
        },
    }


def q_active_alerts(as_of: datetime, replay: str | None = None) -> dict:
    flt = [
        {"term": {"kind": "nws_alert"}},
        {"range": {"valid_from": {"lte": as_of.isoformat()}}},
        {"range": {"valid_to": {"gt": as_of.isoformat()}}},
    ]
    flt.append({"term": {"replay": replay}} if replay else {"bool": {"must_not": {"exists": {"field": "replay"}}}})
    return {"size": 20, "query": {"bool": {"filter": flt}}}


def q_weather(point: str, as_of: datetime, replay: str | None = None, source: str | None = None) -> dict:
    flt = [
        {"term": {"kind": "weather"}},
        {"term": {"point": point}},
        {"range": {"@timestamp": {"gt": (as_of - timedelta(hours=3)).isoformat(), "lte": (as_of + timedelta(hours=3)).isoformat()}}},
    ]
    if source:
        flt.append({"term": {"source": source}})
    flt.append({"term": {"replay": replay}} if replay else {"bool": {"must_not": {"exists": {"field": "replay"}}}})
    return {"size": 20, "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": flt}}}


def q_tides(as_of: datetime, replay: str | None = None) -> dict:
    flt = [{"terms": {"kind": ["tide_pred", "tide_obs"]}}, {"range": {"@timestamp": {"gte": (as_of - timedelta(hours=2)).isoformat(), "lte": (as_of + timedelta(hours=14)).isoformat()}}}]  # fmt: skip
    flt.append({"term": {"replay": replay}} if replay else {"bool": {"must_not": {"exists": {"field": "replay"}}}})
    return {"size": 200, "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": flt}}}


def esql_block_history(cells: list[str], years: int = 5) -> tuple[str, list]:
    """ES|QL: flooding history around a block (cell + neighbours): totals and the worst days."""
    quoted = ", ".join(f'"{c}"' for c in cells)
    query = f"""
FROM {{index}}
| WHERE category == "flooding" AND h3_9 IN ({quoted}) AND requested_at >= NOW() - {years} years
| EVAL day = DATE_TRUNC(1 day, requested_at)
| STATS reports = COUNT(*) BY day
| SORT reports DESC
| LIMIT 5
"""
    return query, []


# ---------------------------------------------------------------- executors


def zone_at(es: ES, lat: float, lon: float) -> dict:
    hits = es.search("flood-zones", **q_zone_at(lat, lon))["hits"]["hits"]
    layers = {h["_source"]["layer"]: h["_source"] for h in hits}
    return {
        "in_100yr": "100yr" in layers,
        "slr24": "slr24" in layers,
        "neighborhood": (layers.get("neighborhood") or {}).get("name"),
    }


def nearby_events(es: ES, lat: float, lon: float, as_of: datetime, hours: float = 6, radius_m: int = 300, replay: str | None = None) -> list[NearbyEvent]:
    since = as_of - timedelta(hours=hours)
    out: list[NearbyEvent] = []
    for h in es.search("hazard-reports", **q_nearby_reports(lat, lon, since, as_of, radius_m))["hits"]["hits"]:
        s = h["_source"]
        cat = s.get("category")
        if cat == "tree":
            if s.get("subcategory") != "fallen_or_damaged":
                continue
            kind = EventKind.tree_report
        elif cat == "street":
            kind = EventKind.street_report
        else:
            kind = FLOOD_KINDS.get(cat)
            if not kind:
                continue
        loc = s["location"]
        vis = s.get("vision") or {}
        out.append(
            NearbyEvent(
                kind=kind,
                at=parse_utc(s["requested_at"]),
                distance_m=round(haversine_m(lat, lon, loc["lat"], loc["lon"])),
                verified_photo=bool(vis.get("water_visible")),
                source="SF311",
                source_url=f"https://mobile311.sfgov.org/reports/{s['case_id']}",
                label=f"{cat}: {s.get('address') or ''}".strip(),
                lat=loc["lat"],
                lon=loc["lon"],
            )
        )
    if not replay:
        for h in es.search("road-incidents", **q_nearby_points("started_at", lat, lon, since, as_of, 800))["hits"]["hits"]:
            s = h["_source"]
            loc = s["location"]
            out.append(
                NearbyEvent(
                    kind=EventKind.road_incident,
                    at=parse_utc(s["started_at"]),
                    distance_m=round(haversine_m(lat, lon, loc["lat"], loc["lon"])),
                    source="CHP",
                    source_url="https://cad.chp.ca.gov/",
                    label=f"{s.get('hazard')}: {s.get('type')} at {s.get('location_desc')}",
                    lat=loc["lat"],
                    lon=loc["lon"],
                )
            )
    sig_q = q_nearby_points("published_at", lat, lon, since, as_of, 500)
    sig_q["query"]["bool"]["filter"].append({"term": {"is_hazard": True}})
    sig_q["query"]["bool"]["filter"].append({"term": {"replay": replay}} if replay else {"bool": {"must_not": {"exists": {"field": "replay"}}}})
    for h in es.search("street-signals", **sig_q)["hits"]["hits"]:
        s = h["_source"]
        loc = s["location"]
        out.append(
            NearbyEvent(
                kind=EventKind.street_signal,
                at=parse_utc(s["published_at"]),
                distance_m=round(haversine_m(lat, lon, loc["lat"], loc["lon"])),
                confidence=float(s.get("confidence") or 0.5),
                source=s.get("source", "public post"),
                source_url=s.get("url"),
                label=(s.get("excerpt") or "")[:140],
                lat=loc["lat"],
                lon=loc["lon"],
            )
        )
    return out


def active_alerts(es: ES, as_of: datetime, replay: str | None = None) -> list[NwsAlert]:
    out = []
    for h in es.search("conditions", **q_active_alerts(as_of, replay))["hits"]["hits"]:
        s = h["_source"]
        out.append(
            NwsAlert(
                id=h["_id"],
                event=s["event"],
                severity=s.get("severity"),
                headline=s.get("headline"),
                starts=parse_utc(s.get("valid_from")),
                ends=parse_utc(s.get("valid_to")),
                url=s.get("url"),
                description=(s.get("description") or "")[:600] or None,
            )
        )
    return out


def weather_at(es: ES, point: str, as_of: datetime, replay: str | None = None) -> Weather | None:
    """Rain last 1h/3h (observed/analysis) and next 3h (forecast) at the nearest weather point."""
    obs_src = "open-meteo-archive" if replay else None
    rows = [h["_source"] for h in es.search("conditions", **q_weather(point, as_of, replay, obs_src))["hits"]["hits"]]
    if replay:  # forecast as it was available at the time (Open-Meteo historical forecast)
        fc_rows = [h["_source"] for h in es.search("conditions", **q_weather(point, as_of, replay, "open-meteo-historical-forecast"))["hits"]["hits"]]
    else:
        fc_rows = rows
    if not rows and not fc_rows:
        return None

    def ts(r):
        return parse_utc(r["@timestamp"])

    past = [r for r in rows if ts(r) <= as_of]
    fut = [r for r in fc_rows if as_of < ts(r) <= as_of + timedelta(hours=3)]
    r1 = sum((r.get("rain_mm") or 0) for r in past if ts(r) > as_of - timedelta(hours=1))
    r3 = sum((r.get("rain_mm") or 0) for r in past if ts(r) > as_of - timedelta(hours=3))
    f3 = sum((r.get("rain_mm") or 0) for r in fut)
    gust_now = max([r.get("gust_kmh") or 0 for r in past if ts(r) > as_of - timedelta(hours=1)] or [0])
    gust_next = max([r.get("gust_kmh") or 0 for r in fut] or [0])
    return Weather(
        point=point,
        rain_1h_mm=round(r1, 2),
        rain_3h_mm=round(r3, 2),
        rain_next3h_mm=round(f3, 2),
        gust_kmh=gust_now,
        gust_next3h_kmh=gust_next,
        as_of=max((ts(r) for r in past), default=as_of),
        source="open-meteo-archive" if replay else "open-meteo",
    )


def tide_at(es: ES, as_of: datetime, replay: str | None = None) -> Tide | None:
    rows = [h["_source"] for h in es.search("conditions", **q_tides(as_of, replay))["hits"]["hits"]]
    if not rows:
        return None
    obs = [r for r in rows if r["kind"] == "tide_obs" and parse_utc(r["@timestamp"]) <= as_of]
    now_ft = obs[-1]["value"] if obs else None
    if replay:  # archive has hourly observations: "next high" = max observed in the next 12 h (as predicted tide tables would show)
        fut = [r for r in rows if r["kind"] == "tide_obs" and as_of < parse_utc(r["@timestamp"]) <= as_of + timedelta(hours=12)]
    else:
        fut = [r for r in rows if r["kind"] == "tide_pred" and r.get("tide_type") == "H" and parse_utc(r["@timestamp"]) > as_of]
    nxt = max(fut, key=lambda r: r["value"]) if replay and fut else (fut[0] if fut else None)
    return Tide(
        now_ft=now_ft,
        next_high_ft=nxt["value"] if nxt else None,
        next_high_at=parse_utc(nxt["@timestamp"]) if nxt else None,
        as_of=parse_utc(obs[-1]["@timestamp"]) if obs else None,
    )


def block_history(es: ES, lat: float, lon: float) -> dict:
    from highground.geo import ring

    cells = ring(cell_of(lat, lon), 1)
    query, params = esql_block_history(cells)
    try:
        rows = es.esql(query.replace("{index}", es.ix("hazard-reports")), params)
    except Exception:  # noqa: BLE001 - ES|QL optional; fall back to a plain aggregation
        rows = []
    return {"top_days": [{"day": r["day"][:10], "reports": r["reports"]} for r in rows]}


def latest_freshness(es: ES) -> dict:
    res = es.search("feed-status", size=50)
    now = datetime.now(UTC)
    out = {}
    for h in res["hits"]["hits"]:
        s = h["_source"]
        last = s.get("last_success")
        age = (now - datetime.fromisoformat(last)).total_seconds() if last else None
        interval = s.get("interval_s") or 0
        out[h["_id"]] = {
            "age_s": round(age) if age is not None else None,
            "interval_s": interval,
            "stale": age is None or (interval and age > 3 * interval),
            "last_error": s.get("last_error") if s.get("consecutive_failures") else None,
        }
    return out
