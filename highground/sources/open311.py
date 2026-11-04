"""SF Open311 GeoReport v2 (real-time, public GET). Live layer for flooding, trees and street hazards."""

from datetime import datetime, timedelta

import httpx

from highground.http import get_json
from highground.models import Category
from highground.sources.hazards import classify_sewer_text, merge_case, parse_utc, to_doc, tree_subcategory

BASE = "https://mobile311.sfgov.org/open311/v2"
SERVICE_CODES = {
    "PUC:Water:Sewer": None,  # flooding / sewer / water leak: classified from description
    "PW:BUF:Tree Maintenance": Category.tree,
    "input:Pothole & Street Issues": Category.street,
}
PAGE_SIZE = 200
MAX_PAGES = 10


def normalize(raw: dict) -> dict | None:
    lat, lon = raw.get("lat"), raw.get("long")
    if lat is None or lon is None or not raw.get("service_request_id") or not raw.get("requested_datetime"):
        return None
    code = raw.get("service_code") or ""
    fixed = SERVICE_CODES.get(code)
    desc = raw.get("description")
    category = fixed or classify_sewer_text(desc)
    sub = tree_subcategory(desc) if category == Category.tree else None
    return to_doc(
        case_id=str(raw["service_request_id"]),
        source="open311",
        category=category,
        requested_at=parse_utc(raw["requested_datetime"]),
        lat=float(lat),
        lon=float(lon),
        subcategory=sub,
        service_name=raw.get("service_name"),
        service_code=code,
        status=raw.get("status"),
        status_notes=raw.get("status_notes"),
        updated_at=parse_utc(raw.get("updated_datetime")),
        address=raw.get("address"),
        description=(desc or "")[:2000] or None,
        media_url=raw.get("media_url") or None,
        category_source="keywords" if fixed is None else "service_code",
    )


class Open311Source:
    name = "open311"
    index = "hazard-reports"
    interval_s = 120
    overlap_s = 15 * 60
    pipeline = "reports-enrich"
    merge = staticmethod(merge_case)

    def __init__(self, base: str = BASE):
        self.base = base

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        start = since or (now - timedelta(days=2))
        docs: list[dict] = []
        for code in SERVICE_CODES:
            for page in range(1, MAX_PAGES + 1):
                rows = get_json(
                    http,
                    f"{self.base}/requests.json",
                    params={
                        "service_code": code,
                        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "page_size": PAGE_SIZE,
                        "page": page,
                    },
                )
                docs.extend(d for d in (normalize(r) for r in rows) if d)
                if len(rows) < PAGE_SIZE:
                    break
        return docs


def submit_request(http: httpx.Client, api_key: str, draft: dict, base: str = BASE) -> dict:
    """Open311 POST /requests (needs an SF311 API key). Only ever called after the user confirms."""
    form = {
        "api_key": api_key,
        "service_code": draft["service_code"],
        "lat": draft["lat"],
        "long": draft["lon"],
        "address_string": draft.get("address") or "",
        "description": draft["description"][:1000],
    }
    if draft.get("media_url"):
        form["media_url"] = draft["media_url"]
    resp = http.post(f"{base}/requests.json", data=form, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body[0] if isinstance(body, list) and body else body
