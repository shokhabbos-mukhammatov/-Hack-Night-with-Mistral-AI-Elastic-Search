"""Receipts check, fallback texts, intents, language detection."""

from datetime import UTC, datetime

from highground import agent, alerts, fallback, verify
from highground.models import Components, Level, Reason, RiskState, ZoneInfo


def state(level=Level.high, codes=("zone_100yr", "reports_nearby")) -> RiskState:
    return RiskState(
        cell="c", level=level, score=2.4, components=Components(),
        reasons=[Reason(code=c, text=f"reason {c}", source="t") for c in codes], as_of=datetime(2026, 11, 3, tzinfo=UTC),
    )  # fmt: skip


def test_verify_drops_invented_numbers_and_keeps_supported():
    facts = {"reasons": ["1.38 in of rain in the last 3 hours.", "3 flooding report(s) within 300 m"]}
    text = "About 1.38 in of rain fell. 3 neighbors reported flooding. Flooding will reach 4 feet by 9:45."
    kept, dropped, gone = verify.check(text, facts)
    assert dropped == 1 and "4 feet" in gone[0] and "1.38" in kept


def test_verify_allows_unit_conversion_ordinals_and_service_numbers():
    facts = {"reasons": ["35.0 mm in 3 hours"]}
    kept, dropped, _ = verify.check("That's 1.38 in. at 17th and Folsom. Call 911 or 311.", facts)
    assert dropped == 0 and kept


def test_actions_by_level():
    high = {a.code for a in fallback.actions_for(state(Level.high), ZoneInfo(in_100yr=True))}
    assert {"911", "turn_around", "move_car", "sandbags", "report"} <= high
    low = {a.code for a in fallback.actions_for(state(Level.low, ()), ZoneInfo(in_100yr=True))}
    assert {"insurance", "grant"} <= low and "911" not in low


def test_alert_text_is_short_and_has_stop():
    txt = fallback.alert_text(state(), "17th & Folsom", "es", "https://example.org/x")
    assert len(txt) <= 320 and "STOP" in txt and "Riesgo alto" in txt


def test_template_labels_localized():
    n = fallback.template_narrative(state(), fallback.actions_for(state(), ZoneInfo()), "es")
    assert n.lang == "es" and "Por qué:" in n.text and "Qué hacer:" in n.text


def test_intents_multilingual():
    assert alerts.parse_intent("alert me for 17th and Folsom") == {"intent": "subscribe", "place": "17th and Folsom"}
    assert alerts.parse_intent("alerta para 24th y Mission")["place"] == "24th y Mission"
    assert alerts.parse_intent("STOP")["intent"] == "stop"
    assert alerts.parse_intent("sí")["intent"] == "confirm"
    assert alerts.parse_intent("where can I get sandbags?")["intent"] == "ask"


def test_detect_lang_and_place_extraction():
    assert agent.detect_lang("¿Dónde consigo sacos de arena?") == "es"
    assert agent.detect_lang("我家附近會水浸嗎") == "zh"
    assert agent.detect_lang("Will my street flood tonight?") == "en"
    assert agent._place_in("Is Cesar Chavez and Mission going to flood?") is not None
    assert agent._place_in("where can I get sandbags?") is None
