"""Shared 311 hazard normalization: categories, keyword classification, and the ES document shape."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from highground.geo import cell_of, in_sf
from highground.models import Category

PACIFIC = ZoneInfo("America/Los_Angeles")

_FLOOD = re.compile(
    r"\b(flood\w*|standing water|ponding|pond|puddl\w*|water (is )?(rising|pooling|in the street|over)|underwater|"
    r"inundat\w*|knee|ankle|curb to curb|lake|river|overflow\w*)\b",
    re.I,
)
_DRAIN = re.compile(r"\b(catch ?basin|storm ?drain|drain\w*|clogged|grate|leaves)\b", re.I)
_SEWER = re.compile(r"\b(sewage|sewer|back ?up|backed up|toilet|odou?r|smell\w*|manhole)\b", re.I)
_LEAK = re.compile(r"\b(leak\w*|water main|hydrant|broken pipe|meter)\b", re.I)
_TREE_DOWN = re.compile(r"\b(fallen|fell|down(ed)?|uproot\w*|blocking|broken|snapped|limb|branch\w*|toppl\w*|leaning)\b", re.I)


def classify_sewer_text(text: str | None) -> Category:
    """Open311 lumps flooding, sewage and leaks into one service code. Keyword rules first; Mistral refines later."""
    t = text or ""
    if _FLOOD.search(t):
        return Category.flooding
    if _SEWER.search(t):
        return Category.sewer
    if _LEAK.search(t):
        return Category.water_leak
    if _DRAIN.search(t):
        return Category.catch_basin
    return Category.other


def tree_subcategory(text: str | None) -> str:
    return "fallen_or_damaged" if _TREE_DOWN.search(text or "") else "maintenance"


def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_pacific(ts: str | None) -> datetime | None:
    """DataSF publishes floating local (Pacific) timestamps."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)
    return dt.astimezone(UTC)


CATEGORY_PRECEDENCE = {"keywords": 0, "service_code": 1, "mistral": 2, "datasf_subtype": 3}
_CATEGORY_FIELDS = {"category", "subcategory", "category_source"}
_DATASF_ONLY = {"neighborhood", "closed_at", "service_details", "on_street", "reporting_channel"}
_DERIVED = {"zone_100yr", "depth_ft", "slr24", "zones"}  # recomputed by the ingest pipeline every time


def merge_case(existing: dict | None, incoming: dict) -> dict:
    """Pure: merge two views of the same 311 case (Open311 live, DataSF nightly, Mistral refinements).

    - category: highest-precedence source wins (DataSF subtype > Mistral > service code > keywords)
    - status / status_notes / updated_at: the most recently updated view wins
    - DataSF-only fields are never erased by a live poll
    """
    if not existing:
        return {k: v for k, v in incoming.items() if k not in _DERIVED}
    out = {k: v for k, v in existing.items() if k not in _DERIVED}
    inc_rank = CATEGORY_PRECEDENCE.get(incoming.get("category_source", "keywords"), 0)
    cur_rank = CATEGORY_PRECEDENCE.get(existing.get("category_source", "keywords"), 0)
    newer = (incoming.get("updated_at") or "") >= (existing.get("updated_at") or "")
    for k, v in incoming.items():
        if k in _DERIVED or k == "_id":
            continue
        if k in _CATEGORY_FIELDS:
            if inc_rank >= cur_rank:
                out[k] = v
        elif k in ("status", "status_notes", "updated_at"):
            if newer:
                out[k] = v
        else:
            out[k] = v
    return out


def to_doc(
    *,
    case_id: str,
    source: str,
    category: Category,
    requested_at: datetime,
    lat: float,
    lon: float,
    **fields,
) -> dict | None:
    """ES document for hg-hazard-reports. _id is shared by both sources so they merge into one case."""
    if not in_sf(lat, lon):
        return None
    doc = {
        "_id": f"311:{case_id}",
        "case_id": case_id,
        "category": category.value,
        "requested_at": requested_at.isoformat(),
        "location": {"lat": lat, "lon": lon},
        "h3_9": cell_of(lat, lon),
        f"seen_{source}": True,
    }
    for k, v in fields.items():
        if v is None:
            continue
        doc[k] = v.isoformat() if isinstance(v, datetime) else v
    return doc
