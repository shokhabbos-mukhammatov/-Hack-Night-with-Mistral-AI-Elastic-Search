"""Report a hazard: photo / voice / text -> moderation -> classification -> duplicates -> structured 311 draft.

Never submits on its own. The user confirms; then we hand off to SF311 (or Open311 POST once we have a key).
Photos are held in memory only for this request.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from highground import http as http_mod
from highground.config import settings
from highground.es import ES
from highground.geo import haversine_m
from highground.geocode import reverse
from highground.mistral_client import MistralUnavailable, mistral
from highground.sources import open311
from highground.sources.hazards import classify_sewer_text

HazardKind = Literal["flooded_street", "clogged_drain", "sewage_backup", "fallen_tree", "road_damage", "not_a_hazard", "unclear"]

SERVICE = {
    "flooded_street": ("PUC:Water:Sewer", "Flooding, sewer and water leak issues", "flooding"),
    "clogged_drain": ("PUC:Water:Sewer", "Flooding, sewer and water leak issues", "catch_basin"),
    "sewage_backup": ("PUC:Water:Sewer", "Flooding, sewer and water leak issues", "sewer_backup"),
    "fallen_tree": ("PW:BUF:Tree Maintenance", "Tree maintenance", "tree"),
    "road_damage": ("input:Pothole & Street Issues", "Pothole and street issues", "street"),
}
SF311_HANDOFF = "https://sf311.org"


class PhotoAssessment(BaseModel):
    kind: HazardKind
    water_visible: bool = Field(description="True only if standing or flowing water is clearly visible")
    blocking: bool = Field(description="True if it blocks a sidewalk, crosswalk, bike lane or traffic lane")
    severity: Literal["low", "medium", "high"]
    description: str = Field(description="One factual sentence a city crew can act on; no names, faces or plates")
    inappropriate: bool = Field(description="True if the image contains nudity, gore or unrelated personal content")
    confidence: float = Field(ge=0, le=1)


VISION_PROMPT = (
    "You help San Francisco residents report street hazards to 311. Look at the photo and classify the hazard. "
    "Describe only what is visible (water depth relative to the curb, blocked drain grate, fallen limb size). "
    "Do not identify people, faces or license plates."
)


def classify_text(text: str) -> HazardKind:
    t = (text or "").lower()
    if any(w in t for w in ("tree", "limb", "branch", "árbol", "arbol", "樹", "树")):
        return "fallen_tree"
    if any(w in t for w in ("pothole", "sinkhole", "bache", "hoyo")):
        return "road_damage"
    cat = classify_sewer_text(text)
    return {"flooding": "flooded_street", "catch_basin": "clogged_drain", "sewer_backup": "sewage_backup"}.get(cat.value, "unclear")


def duplicates(es: ES, lat: float, lon: float, category: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    res = es.search(
        "hazard-reports",
        size=5,
        sort=[{"requested_at": "desc"}],
        query={
            "bool": {
                "filter": [
                    {"geo_distance": {"distance": "75m", "location": {"lat": lat, "lon": lon}}},
                    {"range": {"requested_at": {"gte": (now - timedelta(hours=3)).isoformat()}}},
                    {"term": {"category": category}},
                ]
            }
        },
    )
    out = []
    for h in res["hits"]["hits"]:
        s = h["_source"]
        loc = s["location"]
        out.append(
            {
                "case_id": s["case_id"],
                "requested_at": s["requested_at"],
                "status": s.get("status"),
                "distance_m": round(haversine_m(lat, lon, loc["lat"], loc["lon"])),
                "address": s.get("address"),
            }
        )
    return out


def draft_report(
    es: ES,
    lat: float,
    lon: float,
    *,
    photo: bytes | None = None,
    photo_mime: str = "image/jpeg",
    audio: bytes | None = None,
    text: str | None = None,
    chosen_kind: HazardKind | None = None,
) -> dict:
    m = mistral()
    notes: list[str] = []
    transcript = None
    if audio and m.enabled:
        try:
            transcript = m.transcribe(audio)
            text = " ".join(filter(None, [text, transcript["text"]]))
        except MistralUnavailable:
            notes.append("voice transcription unavailable")
    if text and m.enabled:
        try:
            mod = m.moderate([text])[0]
            if mod["flagged"]:
                return {"ok": False, "reason": "moderation", "categories": mod["categories"]}
        except MistralUnavailable:
            notes.append("text moderation unavailable")
    assessment: PhotoAssessment | None = None
    if photo and m.enabled:
        try:
            assessment = m.vision_parse(VISION_PROMPT + (f" Resident's note: {text}" if text else ""), photo, PhotoAssessment, mime=photo_mime)
            if assessment.inappropriate:
                return {"ok": False, "reason": "moderation", "categories": ["image"]}
        except MistralUnavailable:
            notes.append("photo classification unavailable; please choose the hazard type")
    kind: HazardKind = chosen_kind or (assessment.kind if assessment else classify_text(text or ""))
    if kind in ("not_a_hazard", "unclear") and not chosen_kind:
        return {
            "ok": True,
            "needs_choice": True,
            "assessment": assessment.model_dump() if assessment else None,
            "choices": list(SERVICE),
            "notes": notes,
        }
    code, name, category = SERVICE.get(kind, SERVICE["flooded_street"])
    address = reverse(es, lat, lon)
    description = (assessment.description if assessment else None) or (text or "").strip() or name
    if assessment and assessment.water_visible:
        description = f"{description} (water visible in photo)"
    draft = {
        "id": str(uuid.uuid4()),
        "service_code": code,
        "service_name": name,
        "category": category,
        "description": description[:900],
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "address": address,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return {
        "ok": True,
        "draft": draft,
        "assessment": assessment.model_dump() if assessment else None,
        "transcript": transcript,
        "duplicates": duplicates(es, lat, lon, category),
        "handoff_url": SF311_HANDOFF,
        "can_submit_directly": bool(settings().open311_api_key),
        "notes": notes,
    }


def submit(draft: dict) -> dict:
    cfg = settings()
    if not cfg.open311_api_key:
        return {"ok": False, "reason": "no_open311_key", "handoff_url": SF311_HANDOFF}
    with http_mod.client(timeout=30) as http:
        res = open311.submit_request(http, cfg.open311_api_key, draft)
    return {"ok": True, "service_request_id": res.get("service_request_id") or res.get("token"), "raw": res}
