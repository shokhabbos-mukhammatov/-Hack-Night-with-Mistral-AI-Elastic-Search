from datetime import UTC, datetime, timedelta

from highground import alerts, guides, tools
from highground.geocode import q_places
from highground.messaging.base import classify_http_error
from highground.messaging.photon import normalize_event
from highground.messaging.telegram import parse_update
from highground.models import Components, Level, RiskState

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def st(level):
    return RiskState(cell="c", level=level, score=2.0, components=Components(), as_of=NOW)


def test_should_alert_rises_and_cooldown():
    sub = {"status": "active", "min_level_rank": Level.high.rank}
    assert alerts.should_alert_risk(sub, "c", st(Level.high), NOW)
    assert not alerts.should_alert_risk(sub, "c", st(Level.elevated), NOW)
    recent = {**sub, "last_alert": {"level_rank": Level.high.rank, "at": (NOW - timedelta(minutes=30)).isoformat()}}
    assert not alerts.should_alert_risk(recent, "c", st(Level.high), NOW)
    assert alerts.should_alert_risk(recent, "c", st(Level.severe), NOW), "a rise always alerts"
    old = {**sub, "last_alert": {"level_rank": Level.high.rank, "at": (NOW - timedelta(hours=4)).isoformat()}}
    assert alerts.should_alert_risk(old, "c", st(Level.high), NOW)
    assert not alerts.should_alert_risk({**sub, "status": "pending"}, "c", st(Level.severe), NOW)


def test_backoff_and_http_classification():
    assert alerts.backoff(1) == timedelta(seconds=30) and alerts.backoff(20) == timedelta(hours=1)
    assert classify_http_error(503).retryable and classify_http_error(429).retryable
    assert classify_http_error(410).invalid_handle and classify_http_error(None).retryable


def test_handles_encrypt_roundtrip():
    tok = alerts.encrypt_handle({"chat_id": 42})
    assert tok.startswith("gAAAA") and "chat_id" not in tok  # Fernet token, plaintext not visible
    assert alerts.decrypt_handle(tok) == {"chat_id": 42}
    assert alerts.decrypt_handle("garbage") is None


def test_percolator_query_shape():
    q = alerts.percolator_query(37.76, -122.41, 300)
    geo = q["bool"]["filter"][0]["geo_distance"]
    assert geo["distance"] == "300m" and geo["ev_location"] == {"lat": 37.76, "lon": -122.41}


def test_photon_and_telegram_parsing():
    ev = {"type": "message.received", "data": {"object": {"id": "m1", "platform": "imessage", "status": "received",
          "content": {"type": "group", "parts": [{"type": "text", "text": "alert me for 17th and Folsom"}]},
          "receivedBy": {"id": "agent"}, "space": {"id": "space"}, "sentBy": {"id": "u", "handle": "+14155550100"}}}}  # fmt: skip
    msg = normalize_event(ev)
    assert msg["space_id"] == "space" and msg["agent_user_id"] == "agent" and msg["text"].startswith("alert me")
    assert normalize_event({"type": "message.sent"}) is None
    u = parse_update({"message": {"chat": {"id": 7}, "text": "/start alert_17TH_ST_&_FOLSOM_ST", "from": {"language_code": "es-MX"}}})
    assert u == {"chat_id": 7, "text": "alert 17TH ST & FOLSOM ST", "lang": "es"}


def test_query_builders():
    z = tools.q_zone_at(37.76, -122.41)
    assert z["query"]["geo_shape"]["geometry"]["shape"] == {"type": "point", "coordinates": [-122.41, 37.76]}
    n = tools.q_nearby_reports(37.76, -122.41, NOW - timedelta(hours=6), NOW, 300)
    assert n["query"]["bool"]["filter"][0]["geo_distance"]["distance"] == "300m"
    a = tools.q_active_alerts(NOW)
    assert any("must_not" in f.get("bool", {}) for f in a["query"]["bool"]["filter"]), "live queries exclude replay docs"
    assert {"term": {"replay": "2022-12-31"}} in tools.q_active_alerts(NOW, "2022-12-31")["query"]["bool"]["filter"]
    q, _ = tools.esql_block_history(["a", "b"])
    assert 'h3_9 IN ("a", "b")' in q and "STATS reports = COUNT(*) BY day" in q
    rrf = guides.q_search("sandbags", "es", 4, semantic=True)
    assert "rrf" in rrf["retriever"] and len(rrf["retriever"]["rrf"]["retrievers"]) == 2
    assert "standard" in guides.q_search("sandbags", "es", 4, semantic=False)["retriever"]
    assert q_places("17th and Folsom st")["query"]["bool"]["must"][0]["match"]["alt"]["query"] == "17TH & FOLSOM"
