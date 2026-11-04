"""National Weather Service: active alerts for San Francisco zones (live) and IEM's VTEC archive (replay)."""

from datetime import datetime

import httpx

from highground.http import get_json
from highground.sources.hazards import parse_utc

# San Francisco (city), SF Bay Shoreline, SF Peninsula Coast, SF County
SF_UGC = ("CAZ006", "CAZ508", "CAZ509", "CAC075")
ALERTS = "https://api.weather.gov/alerts/active"
IEM = "https://mesonet.agron.iastate.edu/json/vtec_events_bywfo.py"

# VTEC phenomena/significance -> NWS event name (subset relevant to flooding, coast and wind/trees)
VTEC_EVENTS = {
    ("FA", "A"): "Flood Watch",
    ("FA", "W"): "Flood Warning",
    ("FA", "Y"): "Flood Advisory",
    ("FF", "A"): "Flash Flood Watch",
    ("FF", "W"): "Flash Flood Warning",
    ("FL", "W"): "Flood Warning",
    ("CF", "Y"): "Coastal Flood Advisory",
    ("CF", "A"): "Coastal Flood Watch",
    ("CF", "W"): "Coastal Flood Warning",
    ("CF", "S"): "Coastal Flood Statement",
    ("WI", "Y"): "Wind Advisory",
    ("HW", "W"): "High Wind Warning",
    ("HW", "A"): "High Wind Watch",
}
RELEVANT_EVENTS = set(VTEC_EVENTS.values()) | {"Beach Hazards Statement", "Hydrologic Outlook"}


def parse_alerts(payload: dict) -> list[dict]:
    out = []
    for f in payload.get("features", []):
        p = f.get("properties", {})
        if p.get("event") not in RELEVANT_EVENTS:
            continue
        starts = parse_utc(p.get("onset") or p.get("effective"))
        ends = parse_utc(p.get("ends") or p.get("expires"))
        out.append(
            {
                "_id": f"nws:{p['id']}",
                "@timestamp": (parse_utc(p.get("sent")) or starts).isoformat(),
                "kind": "nws_alert",
                "event": p.get("event"),
                "severity": p.get("severity"),
                "headline": p.get("headline"),
                "description": (p.get("description") or "")[:3000],
                "area": p.get("areaDesc"),
                "valid_from": starts.isoformat() if starts else None,
                "valid_to": ends.isoformat() if ends else None,
                "url": p.get("@id") or f.get("id"),
                "source": "nws",
            }
        )
    return out


class NwsAlertSource:
    name = "nws-alerts"
    index = "conditions"
    interval_s = 5 * 60
    overlap_s = 0
    pipeline = None

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        payload = get_json(
            http, ALERTS, params={"zone": ",".join(SF_UGC)}, headers={"Accept": "application/geo+json"}
        )
        return parse_alerts(payload)


def parse_iem(payload: dict, start: str, end: str) -> list[dict]:
    """Pure: archived VTEC events overlapping [start, end) for SF zones, as nws_alert docs."""
    seen: dict[str, dict] = {}
    for e in payload.get("events", []):
        if e.get("ugc") not in SF_UGC:
            continue
        event = VTEC_EVENTS.get((e.get("phenomena"), e.get("significance")))
        if not event:
            continue
        if not (e["issue"] < end and e["expire"] > start):
            continue
        key = f"{e['phenomena']}.{e['significance']}.{e['eventid']}"
        doc = seen.setdefault(
            key,
            {
                "_id": f"iem:{e.get('wfo', 'MTR')}:{e['issue'][:4]}:{key}",
                "@timestamp": e.get("product_issued") or e["issue"],
                "kind": "nws_alert",
                "event": event,
                "headline": f"{event} (archived NWS product, event {e['eventid']})",
                "valid_from": e["issue"],
                "valid_to": e["expire"],
                "issued_at": e.get("product_issued") or e["issue"],
                "ugc": [],
                "url": e.get("url"),
                "source": "iem-vtec-archive",
            },
        )
        doc["ugc"].append(e["ugc"])
    return list(seen.values())


def archive(http: httpx.Client, year: int, start: str, end: str) -> list[dict]:
    return parse_iem(get_json(http, IEM, params={"wfo": "MTR", "year": year}, timeout=60), start, end)
