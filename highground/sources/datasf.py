"""DataSF 311 Cases (vw6y-z8j6): full history, refreshed nightly ~6 AM PT. Authoritative subtypes."""

from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx

from highground.http import get_json
from highground.models import Category
from highground.sources.hazards import PACIFIC, merge_case, parse_pacific, to_doc

DATASET = "https://data.sf.gov/resource/vw6y-z8j6.json"
SERVICE_NAMES = (
    "Sewer Issues",
    "Sewer",
    "Catch Basin Maintenance",
    "Tree Maintenance",
    "Street Defects",
    "Blocked Street or SideWalk",
)
PAGE = 50_000

_SEWER_SUBTYPES = {
    "flooding": Category.flooding,
    "sewage_back_up": Category.sewer,
    "sewage_back-up_discharge": Category.sewer,
    "side_sewer_issues": Category.sewer,
    "odor": Category.sewer,
    "sewer_odor": Category.sewer,
    "water_leak": Category.water_leak,
    "water_main_break": Category.water_leak,
    "catch_basin": Category.catch_basin,
    "catch_basin_other": Category.catch_basin,
    "garbage_and_debris": Category.catch_basin,
}


def categorize(service_name: str, subtype: str | None) -> tuple[Category, str | None] | None:
    st = (subtype or "").strip().lower()
    if service_name in ("Sewer Issues", "Sewer"):
        return _SEWER_SUBTYPES.get(st, Category.other), st or None
    if service_name == "Catch Basin Maintenance":
        return Category.catch_basin, st or None
    if service_name == "Tree Maintenance":
        if any(k in st for k in ("damaged", "damaging", "tree_other")):
            return Category.tree, "fallen_or_damaged"
        return None  # overgrown / landscaping: not a storm hazard
    if service_name == "Street Defects":
        return Category.street, st or "pavement"
    if service_name == "Blocked Street or SideWalk":
        return Category.street, "blocked"
    return None


def normalize(raw: dict) -> dict | None:
    lat, lon = raw.get("lat"), raw.get("long")
    if not lat or not lon or not raw.get("requested_datetime"):
        return None
    cat = categorize(raw.get("service_name", ""), raw.get("service_subtype"))
    if not cat:
        return None
    category, sub = cat
    details = raw.get("service_details") or ""
    return to_doc(
        case_id=str(raw["service_request_id"]),
        source="datasf",
        category=category,
        requested_at=parse_pacific(raw["requested_datetime"]),
        lat=float(lat),
        lon=float(lon),
        subcategory=sub,
        service_name=raw.get("service_name"),
        service_details=details or None,
        on_street="on_street" in details.lower() if details else None,
        status=(raw.get("status_description") or "").lower() or None,
        status_notes=raw.get("status_notes"),
        updated_at=parse_pacific(raw.get("updated_datetime")),
        closed_at=parse_pacific(raw.get("closed_date")),
        address=raw.get("address"),
        neighborhood=raw.get("analysis_neighborhood"),
        reporting_channel=raw.get("source"),
        category_source="datasf_subtype",
    )


def _where(extra: str) -> str:
    names = ",".join(f"'{n}'" for n in SERVICE_NAMES)
    return f"service_name in ({names}) AND {extra}"


def iter_rows(http: httpx.Client, where: str, page: int = PAGE) -> Iterator[dict]:
    offset = 0
    while True:
        rows = get_json(
            http,
            DATASET,
            params={"$where": where, "$order": ":id", "$limit": page, "$offset": offset},
            timeout=120,
        )
        yield from rows
        if len(rows) < page:
            return
        offset += page


def local_iso(dt: datetime) -> str:
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%dT%H:%M:%S")


class DataSFSource:
    """Nightly sync of cases updated since the watermark (also fills subtypes, closures, neighborhoods)."""

    name = "datasf"
    index = "hazard-reports"
    interval_s = 24 * 3600
    overlap_s = 6 * 3600
    pipeline = "reports-enrich"
    merge = staticmethod(merge_case)

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        start = since or (now - timedelta(days=3))
        where = _where(f"updated_datetime >= '{local_iso(start)}'")
        return [d for d in (normalize(r) for r in iter_rows(http, where)) if d]


def backfill(http: httpx.Client, start: datetime, end: datetime | None = None) -> Iterator[dict]:
    """History load, e.g. the last 5 years. Yields normalized docs."""
    cond = f"requested_datetime >= '{local_iso(start)}'"
    if end:
        cond += f" AND requested_datetime < '{local_iso(end)}'"
    for r in iter_rows(http, _where(cond)):
        d = normalize(r)
        if d:
            yield d
