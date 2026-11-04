"""Block Briefing orchestration: point -> cell -> live inputs -> risk (pure) -> actions -> narrative (cached)."""

import hashlib
from datetime import UTC, datetime, timedelta

from elasticsearch import NotFoundError

from highground import agent, fallback, tools
from highground.es import ES
from highground.geo import cell_of
from highground.inputs import LiveInputs
from highground.models import Briefing, Narrative, RiskState
from highground.risk import assess

NARRATIVE_TTL = timedelta(minutes=10)


def build_briefing(es: ES, lat: float, lon: float, lang: str = "en", label: str | None = None, narrate_with_llm: bool = True) -> Briefing:
    now = datetime.now(UTC)
    cell = cell_of(lat, lon)
    inputs = LiveInputs(es).inputs_for(cell, now, (lat, lon))
    state = assess(inputs)
    point_zone = tools.zone_at(es, lat, lon)
    zone = inputs.profile.zone.model_copy(update={"in_100yr": inputs.profile.zone.in_100yr or point_zone["in_100yr"]})
    actions = fallback.actions_for(state, zone)
    history = tools.block_history(es, lat, lon)
    conditions = {
        "weather": inputs.weather.model_dump(mode="json") if inputs.weather else None,
        "tide": inputs.tide.model_dump(mode="json") if inputs.tide else None,
        "alerts": _dedupe_alerts([a.model_dump(mode="json", exclude={"description"}) for a in inputs.alerts]),
    }
    facts = {
        "place": label or point_zone.get("neighborhood") or "this block",
        "neighborhood": point_zone.get("neighborhood"),
        "risk_level": state.level.value,
        "reasons": [r.text for r in state.reasons],
        "actions": [{"text": a.text, "url": a.url} for a in actions],
        "exact_point_in_100yr_zone": point_zone["in_100yr"],
        "worst_days_nearby": history.get("top_days", []),
        "alerts": [f"{a['event']} until {a['ends']}" for a in conditions["alerts"]],
        "_state": state,
        "_actions": actions,
    }
    if narrate_with_llm:
        narrative = cached_narrative(es, cell, lang, state, facts)
    else:
        narrative = fallback.template_narrative(state, actions, lang)
    return Briefing(
        cell=cell,
        location={"lat": lat, "lon": lon, "label": label, "neighborhood": point_zone.get("neighborhood"), "exact_point": point_zone},
        risk=state,
        actions=actions,
        zone=zone,
        history=inputs.profile.model_copy(update={"top_dates": [d["day"] for d in history.get("top_days", [])]}),
        conditions=conditions,
        nearby=sorted(inputs.events, key=lambda e: e.at, reverse=True)[:25],
        narrative=narrative,
        freshness=tools.latest_freshness(es),
        disclaimer=fallback.DISCLAIMER,
    )


def cached_narrative(es: ES, cell: str, lang: str, state: RiskState, facts: dict) -> Narrative:
    key = hashlib.sha1(f"{cell}|{lang}|{state.level}|{state.inputs_hash}".encode()).hexdigest()
    now = datetime.now(UTC)
    try:
        doc = es.client.get(index=es.ix("narratives"), id=key)["_source"]
        if datetime.fromisoformat(doc["expires_at"]) > now:
            return Narrative(**doc["narrative"])
    except NotFoundError:
        pass
    narrative = agent.narrate(facts, lang)
    if narrative.generated_by == "mistral":
        es.client.index(
            index=es.ix("narratives"),
            id=key,
            document={"cell": cell, "lang": lang, "state_hash": state.inputs_hash, "narrative": narrative.model_dump(),
                      "created_at": now.isoformat(), "expires_at": (now + NARRATIVE_TTL).isoformat()},  # fmt: skip
        )
    return narrative


def _dedupe_alerts(alerts: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in alerts:
        k = (a["event"], a.get("ends"))
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out
