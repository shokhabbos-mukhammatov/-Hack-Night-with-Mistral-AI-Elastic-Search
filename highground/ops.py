"""City-ops analytics: ES|QL LOOKUP JOIN hotspots, significant_terms "unusual now", ES|QL COMPLETION summaries.

These power /ops (a live operations view for city crews and community organizations) and agent tools.
"""

import logging
from datetime import UTC, datetime, timedelta

from highground.es import COMPLETION_ENDPOINT, ES

log = logging.getLogger("highground.ops")
FLOOD = ("flooding", "catch_basin", "sewer_backup")


def esql_hotspots(reports_index: str, profiles_index: str, hours: int = 6, limit: int = 20) -> str:
    """Pure: recent reports per block, joined (LOOKUP JOIN) with each block's flood zone and storm history."""
    cats = ", ".join(f'"{c}"' for c in FLOOD)
    return f"""
FROM {reports_index}
| WHERE requested_at >= NOW() - {int(hours)} hours AND category IN ({cats})
| STATS reports = COUNT(*), latest = MAX(requested_at), neighborhood = TOP(neighborhood, 1, "asc") BY h3_9
| LOOKUP JOIN {profiles_index} ON h3_9
| EVAL weight = CASE(in_100yr == true, 2.0, 1.0) + COALESCE(storm_reports_5y, 0) / 20.0
| EVAL priority = ROUND(reports * weight, 2)
| KEEP h3_9, neighborhood, reports, latest, in_100yr, storm_reports_5y, priority
| SORT priority DESC, latest DESC
| LIMIT {int(limit)}
"""


def hotspots(es: ES, hours: int = 6, limit: int = 20) -> list[dict]:
    rows = es.esql(esql_hotspots(es.ix("hazard-reports"), es.ix("cell-profiles"), hours, limit))
    for r in rows:
        if isinstance(r.get("neighborhood"), list):
            r["neighborhood"] = r["neighborhood"][0] if r["neighborhood"] else None
    return rows


def q_unusual(now: datetime, hours: int = 6, background_days: int = 30) -> dict:
    """Pure: significant_terms of neighborhood and category: what's over-represented now vs the last month.
    Explicit timestamps (not `now`) because background filters must be cacheable."""
    fg = (now - timedelta(hours=hours)).isoformat()
    bg = (now - timedelta(days=background_days)).isoformat()
    sig = {"background_filter": {"range": {"requested_at": {"gte": bg}}}, "size": 5, "min_doc_count": 3}
    return {
        "size": 0,
        "query": {"range": {"requested_at": {"gte": fg}}},
        "aggs": {
            "neighborhoods": {"significant_terms": {"field": "neighborhood", **sig}},
            "categories": {"significant_terms": {"field": "category", **sig}},
        },
    }


def unusual(es: ES, now: datetime | None = None, hours: int = 6) -> dict:
    now = now or datetime.now(UTC)
    aggs = es.search("hazard-reports", **q_unusual(now, hours))["aggregations"]
    return {
        k: [{"key": b["key"], "count": b["doc_count"], "background": b["bg_count"], "score": round(b["score"], 2)} for b in aggs[k]["buckets"]]
        for k in ("neighborhoods", "categories")
    }


def esql_neighborhood(reports_index: str, with_completion: str | None, hours: int = 6) -> str:
    """Pure: counts for one neighborhood; optionally an LLM summary inside ES|QL (COMPLETION, Mistral endpoint)."""
    q = f"""
FROM {reports_index}
| WHERE neighborhood == ?nh AND requested_at >= NOW() - {int(hours)} hours
| STATS flooding = COUNT(*) WHERE category == "flooding", drains = COUNT(*) WHERE category == "catch_basin",
        sewer = COUNT(*) WHERE category == "sewer_backup", trees = COUNT(*) WHERE category == "tree", total = COUNT(*)
| LIMIT 1
"""
    if with_completion:
        q += f"""| EVAL prompt = CONCAT("In one calm, plain sentence for residents of ", ?nh, ", San Francisco, summarize the last {int(hours)} hours of 311 reports: ",
    TO_STRING(flooding), " flooding, ", TO_STRING(drains), " clogged drains, ", TO_STRING(sewer), " sewer backups, ", TO_STRING(trees),
    " tree problems. Use only these numbers. Do not give advice.")
| COMPLETION summary = prompt WITH {{ "inference_id": "{with_completion}" }}
| KEEP flooding, drains, sewer, trees, total, summary
"""
    return q


def neighborhood(es: ES, name: str, hours: int = 6) -> dict:
    params = [{"nh": name}]
    endpoint = es.ix(COMPLETION_ENDPOINT) if es.cfg.has_mistral else None
    rows = []
    if endpoint:
        try:
            rows = es.esql(esql_neighborhood(es.ix("hazard-reports"), endpoint, hours), params)
        except Exception as exc:  # noqa: BLE001 - COMPLETION unavailable -> plain stats
            log.info("ES|QL COMPLETION unavailable: %s", exc)
            rows = []
    if not rows:
        rows = es.esql(esql_neighborhood(es.ix("hazard-reports"), None, hours), params)
    r = rows[0] if rows else {"flooding": 0, "drains": 0, "sewer": 0, "trees": 0, "total": 0}
    if not r.get("summary"):
        r["summary"] = (
            f"In the last {hours} hours, {name} had {r['flooding']} flooding, {r['drains']} clogged-drain, "
            f"{r['sewer']} sewer-backup and {r['trees']} tree reports to 311."
        )
        r["summary_by"] = "template"
    else:
        r["summary_by"] = "mistral via ES|QL COMPLETION"
    return {"neighborhood": name, "hours": hours, **r}


def outbox_stats(es: ES) -> dict:
    res = es.search("alerts-outbox", size=0, aggs={"s": {"terms": {"field": "status"}}})
    return {b["key"]: b["doc_count"] for b in res["aggregations"]["s"]["buckets"]}
