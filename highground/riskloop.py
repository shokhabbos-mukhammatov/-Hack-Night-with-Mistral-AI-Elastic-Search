"""Live risk loop (worker): snapshot ES -> same ReplayInputs + risk engine for every cell -> hg-risk-cells -> alerts.

Using the replay's in-memory provider for live too means live and replay cannot drift apart.
"""

import logging
from datetime import UTC, datetime, timedelta

from elasticsearch.helpers import scan

from highground import alerts
from highground.es import ES
from highground.geo import cell_center, cell_polygon_geojson
from highground.models import Level, RiskState
from highground.replay import ReplayDataset, ReplayInputs
from highground.risk import assess, smooth_level

log = logging.getLogger("highground.riskloop")
FLOOD_CATS = ("flooding", "catch_basin", "sewer_backup")


def live_snapshot(es: ES, now: datetime) -> ReplayDataset:
    profiles = {}
    for h in scan(es.client, index=es.ix("cell-profiles"), query={"query": {"match_all": {}}}, size=2000):
        s = h["_source"]
        profiles[s["cell"]] = {
            "zone": {"in_100yr": s.get("in_100yr", False), "slr24": s.get("slr24", False), "shoreline": s.get("shoreline", False), "depth_ft": None},
            "reports_5y": s.get("reports_5y", 0),
            "storm_reports_5y": s.get("storm_reports_5y", 0),
            "storm_days_5y": s.get("storm_days_5y", 0),
        }
    not_replay = {"bool": {"must_not": {"exists": {"field": "replay"}}}}
    wx = es.search(
        "conditions",
        size=500,
        sort=[{"@timestamp": "asc"}],
        query={"bool": {"filter": [{"term": {"kind": "weather"}}, not_replay,
                                   {"range": {"@timestamp": {"gte": (now - timedelta(hours=4)).isoformat(), "lte": (now + timedelta(hours=4)).isoformat()}}}]}},
    )["hits"]["hits"]  # fmt: skip
    obs, fc = {}, {}
    for h in wx:
        s = h["_source"]
        row = (s["@timestamp"], s.get("rain_mm") or 0.0, s.get("gust_kmh") or 0.0)
        (fc if datetime.fromisoformat(s["@timestamp"]) > now else obs).setdefault(s["point"], []).append(row)
    tide = [
        (h["_source"]["@timestamp"], h["_source"]["value"])
        for h in es.search(
            "conditions",
            size=200,
            sort=[{"@timestamp": "asc"}],
            query={"bool": {"filter": [{"terms": {"kind": ["tide_pred", "tide_obs"]}}, not_replay,
                                       {"range": {"@timestamp": {"gte": (now - timedelta(hours=2)).isoformat(), "lte": (now + timedelta(hours=14)).isoformat()}}}]}},
        )["hits"]["hits"]
    ]  # fmt: skip
    al = es.search(
        "conditions",
        size=50,
        query={"bool": {"filter": [{"term": {"kind": "nws_alert"}}, not_replay, {"range": {"valid_to": {"gt": now.isoformat()}}}]}},
    )["hits"]["hits"]
    alerts_ = [
        {"event": s["event"], "valid_from": s.get("valid_from"), "valid_to": s.get("valid_to"), "issued_at": s.get("@timestamp"), "url": s.get("url"), "headline": s.get("headline")}
        for s in (h["_source"] for h in al)
    ]  # fmt: skip
    rep = es.search(
        "hazard-reports",
        size=5000,
        query={"bool": {"filter": [{"range": {"requested_at": {"gte": (now - timedelta(hours=6)).isoformat(), "lte": now.isoformat()}}},
                                   {"terms": {"category": [*FLOOD_CATS, "tree"]}}]}},
    )["hits"]["hits"]  # fmt: skip
    reports = []
    for h in rep:
        s = h["_source"]
        if s["category"] == "tree" and s.get("subcategory") != "fallen_or_damaged":
            continue
        reports.append({"t": s["requested_at"], "lat": s["location"]["lat"], "lon": s["location"]["lon"], "cell": s["h3_9"],
                        "category": s["category"], "address": s.get("address"), "case_id": s["case_id"], "source": "SF311 (Open311)",
                        "verified_photo": bool((s.get("vision") or {}).get("water_visible"))})  # fmt: skip
    extra = []
    for h in es.search("road-incidents", size=200, query={"range": {"started_at": {"gte": (now - timedelta(hours=6)).isoformat()}}})["hits"]["hits"]:
        s = h["_source"]
        extra.append({"t": s["started_at"], "lat": s["location"]["lat"], "lon": s["location"]["lon"], "cell": s["h3_9"], "kind": "road_incident",
                      "label": f"{s.get('hazard')}: {s.get('type')} at {s.get('location_desc')}", "source": "CHP", "url": "https://cad.chp.ca.gov/"})  # fmt: skip
    for h in es.search("street-signals", size=200, query={"bool": {"filter": [{"term": {"is_hazard": True}}, not_replay, {"range": {"published_at": {"gte": (now - timedelta(hours=6)).isoformat()}}}]}})["hits"]["hits"]:
        s = h["_source"]
        extra.append({"t": s["published_at"], "lat": s["location"]["lat"], "lon": s["location"]["lon"], "cell": s["h3_9"], "kind": "street_signal",
                      "label": (s.get("excerpt") or "")[:140], "confidence": s.get("confidence", 0.5), "source": s.get("source"), "url": s.get("url")})  # fmt: skip
    return ReplayDataset(date=now.date().isoformat(), weather_obs=obs, weather_fc=fc, tide=sorted(tide), alerts=alerts_,
                         reports=sorted(reports, key=lambda r: r["t"]), profiles=profiles, extra_events=extra)  # fmt: skip


def run_once(es: ES, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    ds = live_snapshot(es, now)
    prov = ReplayInputs(ds)
    prev = {}
    for h in scan(es.client, index=es.ix("risk-cells"), query={"query": {"match_all": {}}, "_source": ["level", "calm_runs"]}, size=2000):
        prev[h["_id"]] = h["_source"]
    docs, history, rose = [], [], {}
    counts = {lv.value: 0 for lv in Level}
    for cell in prov.cells():
        st = assess(prov.inputs_for(cell, now))
        p = prev.get(cell)
        prev_level = Level(p["level"]) if p else None
        lvl, calm = smooth_level(prev_level, st.level, (p or {}).get("calm_runs", 0))
        st = st.model_copy(update={"level": lvl})
        counts[lvl.value] += 1
        lat, lon = cell_center(cell)
        docs.append({"_id": cell, "cell": cell, "level": lvl.value, "level_rank": lvl.rank, "score": st.score,
                     "components": st.components.model_dump(), "reasons": [r.model_dump(mode="json") for r in st.reasons],
                     "as_of": now.isoformat(), "inputs_hash": st.inputs_hash, "centroid": {"lat": lat, "lon": lon},
                     "polygon": cell_polygon_geojson(cell), "calm_runs": calm})  # fmt: skip
        if prev_level != lvl:
            history.append({"_id": f"{cell}:{now.strftime('%Y%m%d%H%M')}", "@timestamp": now.isoformat(), "cell": cell, "level": lvl.value, "level_rank": lvl.rank, "score": st.score})
            if prev_level is None or lvl.rank > prev_level.rank:
                rose[cell] = st
    es.upsert_docs("risk-cells", docs)
    es.upsert_docs("risk-history", history)
    sent_risk = alerts.risk_alerts(es, {c: s for c, s in rose.items() if s.level.rank >= Level.elevated.rank}, now)
    return {"cells": len(docs), "levels": counts, "changed": len(history), "rose": len(rose), "alerts_enqueued": sent_risk}


def new_events_since(es: ES, since: datetime) -> list[dict]:
    """Hazard events ingested since the last check, shaped for percolation."""
    out = []
    for h in es.search("hazard-reports", size=500, query={"bool": {"filter": [{"range": {"ingested_at": {"gt": since.isoformat()}}}, {"terms": {"category": ["flooding", "catch_basin"]}}, {"range": {"requested_at": {"gte": (since - timedelta(hours=1)).isoformat()}}}]}})["hits"]["hits"]:  # fmt: skip
        s = h["_source"]
        out.append({"id": h["_id"], "location": s["location"], "kind": "flood_report", "label": s.get("address"), "cell": s["h3_9"]})
    for h in es.search("road-incidents", size=100, query={"bool": {"filter": [{"range": {"started_at": {"gt": since.isoformat()}}}, {"term": {"hazard": "flooding"}}]}})["hits"]["hits"]:  # fmt: skip
        s = h["_source"]
        out.append({"id": h["_id"], "location": s["location"], "kind": "road_incident", "label": s.get("location_desc"), "cell": s["h3_9"]})
    return out


def state_summary(st: RiskState) -> dict:
    return {"cell": st.cell, "level": st.level.value, "score": st.score}
