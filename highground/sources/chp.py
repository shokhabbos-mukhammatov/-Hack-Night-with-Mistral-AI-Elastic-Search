"""CHP live incidents (Golden Gate communications center), filtered to San Francisco road hazards."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from highground.geo import cell_of, in_sf
from highground.http import request

FEED = "https://media.chp.ca.gov/sa_xml/sa.xml"
PACIFIC = ZoneInfo("America/Los_Angeles")
HAZARD_TYPES = ("Traffic Hazard", "CLOSURE", "Roadway Flooding", "Flood", "Tree", "Debris", "Wires Down", "SIG Alert")
_FLOOD = re.compile(r"\b(flood\w*|water|ponding|standing water|hydroplan\w*)\b", re.I)
_TREE = re.compile(r"\b(tree|limb|branch)\b", re.I)


def _clean(s: str | None) -> str:
    return (s or "").strip().strip('"').strip()


def _time(s: str) -> datetime:
    return datetime.strptime(" ".join(_clean(s).split()), "%b %d %Y %I:%M%p").replace(tzinfo=PACIFIC)


def parse(xml_text: str, now: datetime) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for log in root.iter("Log"):
        log_type = _clean(log.findtext("LogType"))
        if not any(h.lower() in log_type.lower() for h in HAZARD_TYPES):
            continue
        latlon = _clean(log.findtext("LATLON"))
        if ":" not in latlon:
            continue
        a, b = latlon.split(":")
        try:
            lat, lon = int(a) / 1e6, -int(b) / 1e6
        except ValueError:
            continue
        if not in_sf(lat, lon):
            continue
        details = [_clean(d.findtext("IncidentDetail")) for d in log.iter("details")]
        text = " ".join([log_type, _clean(log.findtext("Location")), *details])
        started = _time(log.findtext("LogTime"))
        out.append(
            {
                "_id": f"chp:{log.get('ID')}",
                "log_id": log.get("ID"),
                "type": log_type,
                "hazard": "flooding" if _FLOOD.search(text) else "tree" if _TREE.search(text) else "road",
                "location": {"lat": lat, "lon": lon},
                "h3_9": cell_of(lat, lon),
                "location_desc": _clean(log.findtext("Location")),
                "area": _clean(log.findtext("Area")),
                "details": details[:10],
                "started_at": started.isoformat(),
                "@timestamp": started.isoformat(),
                "last_seen": now.isoformat(),
                "source": "chp",
            }
        )
    return out


class ChpSource:
    name = "chp"
    index = "road-incidents"
    interval_s = 5 * 60
    overlap_s = 0
    pipeline = None

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        return parse(request(http, "GET", FEED, timeout=30).text, now)
