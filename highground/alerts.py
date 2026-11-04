"""Alert subscriptions (percolator), inbound text intents, the outbox, and the dispatcher.

Trigger 1: a block's risk level rises to the subscriber's minimum   -> term match on subscriptions.cells
Trigger 2: a new hazard event lands near a subscriber's place       -> percolate the event against subscriptions
Every alert goes through hg-alerts-outbox with a deterministic _id, so retries never double-send.
"""

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from elasticsearch import ConflictError, NotFoundError

from highground import fallback
from highground.config import settings
from highground.es import ES
from highground.geo import cell_of, ring
from highground.messaging.base import Channel
from highground.models import Level, RiskState

log = logging.getLogger("highground.alerts")
RISK_COOLDOWN = timedelta(hours=3)
EVENT_COOLDOWN = timedelta(hours=2)
MAX_ATTEMPTS = 5


# ---------------------------------------------------------------- handles (encrypted at rest)


def _fernet() -> Fernet:
    key = settings().fernet_key
    if not key:
        raise RuntimeError("HG_FERNET_KEY is required to store alert subscriptions")
    return Fernet(key.encode())


def encrypt_handle(handle: dict) -> str:
    return _fernet().encrypt(json.dumps(handle, sort_keys=True).encode()).decode()


def decrypt_handle(token: str) -> dict | None:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return None


def handle_hash(channel: str, key: str) -> str:
    return hashlib.sha256(f"{settings().fernet_key}|{channel}|{key}".encode()).hexdigest()


def handle_key(channel: str, handle: dict) -> str:
    return {"imessage": handle.get("space_id"), "telegram": str(handle.get("chat_id")), "webpush": (handle.get("subscription") or {}).get("endpoint")}.get(channel, "")  # fmt: skip


# ---------------------------------------------------------------- subscriptions


def percolator_query(lat: float, lon: float, radius_m: int) -> dict:
    """Pure: the stored query that matches hazard events near the subscriber's place."""
    return {
        "bool": {
            "filter": [
                {"geo_distance": {"distance": f"{radius_m}m", "ev_location": {"lat": lat, "lon": lon}}},
                {"terms": {"ev_kind": ["flood_report", "road_incident", "street_signal", "tree_report"]}},
            ]
        }
    }


def subscribe(
    es: ES, channel: str, handle: dict, lat: float, lon: float, *, label: str | None, lang: str = "en",
    radius_m: int = 300, min_level: Level = Level.high, confirmed: bool = False,
) -> dict:  # fmt: skip
    key = handle_key(channel, handle)
    hh = handle_hash(channel, key)
    sub_id = hashlib.sha1(f"{hh}|{round(lat, 4)}|{round(lon, 4)}".encode()).hexdigest()[:20]
    token = secrets.token_urlsafe(18)
    now = datetime.now(UTC)
    k = 1 if radius_m <= 350 else 2
    doc = {
        "channel": channel,
        "handle_enc": encrypt_handle(handle),
        "handle_hash": hh,
        "lang": lang,
        "label": label,
        "location": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "cells": ring(cell_of(lat, lon), k),
        "min_level": min_level.value,
        "min_level_rank": min_level.rank,
        "status": "active" if confirmed else "pending",
        "created_at": now.isoformat(),
        "confirmed_at": now.isoformat() if confirmed else None,
        "unsub_token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "query": percolator_query(lat, lon, radius_m),
    }
    es.client.index(index=es.ix("subscriptions"), id=sub_id, document=doc, refresh="wait_for")
    return {"subscription_id": sub_id, "status": doc["status"], "unsubscribe_token": token, "cells": doc["cells"]}


def set_status_for_handle(es: ES, channel: str, key: str, status: str) -> int:
    hh = handle_hash(channel, key)
    if status == "stopped":  # STOP deletes: no reason to keep anyone's handle
        res = es.client.delete_by_query(index=es.ix("subscriptions"), query={"term": {"handle_hash": hh}}, refresh=True)
        return res.get("deleted", 0)
    res = es.client.update_by_query(
        index=es.ix("subscriptions"),
        query={"bool": {"filter": [{"term": {"handle_hash": hh}}, {"term": {"status": "pending"}}]}},
        script={"source": "ctx._source.status = params.s; ctx._source.confirmed_at = params.t", "params": {"s": status, "t": datetime.now(UTC).isoformat()}},
        refresh=True,
    )  # fmt: skip
    return res.get("updated", 0)


def unsubscribe_token(es: ES, sub_id: str, token: str) -> bool:
    try:
        doc = es.client.get(index=es.ix("subscriptions"), id=sub_id)["_source"]
    except NotFoundError:
        return False
    if doc.get("unsub_token_hash") != hashlib.sha256(token.encode()).hexdigest():
        return False
    es.client.delete(index=es.ix("subscriptions"), id=sub_id, refresh="wait_for")
    return True


# ---------------------------------------------------------------- inbound text intents

STOP_WORDS = {"stop", "unsubscribe", "cancel", "alto", "parar", "停止", "取消", "huwag", "dừng"}
YES_WORDS = {"yes", "y", "si", "sí", "是", "oo", "có", "da", "confirm"}
_SUB = re.compile(r"(?:alert|alerts|alerta|alertas|notify|avisar|avísame|subscribe|watch|提醒)\b(?:\s+(?:me|for|para|de|me for|me about))*\s*(.+)", re.I)


def parse_intent(text: str) -> dict:
    """Pure, multilingual keyword intents; Mistral is used for anything else (handled in the worker)."""
    t = (text or "").strip()
    low = t.lower().strip(" .!")
    if low in STOP_WORDS or low.startswith("stop"):
        return {"intent": "stop"}
    if low in YES_WORDS:
        return {"intent": "confirm"}
    m = _SUB.search(t)
    if m and m.group(1).strip():
        return {"intent": "subscribe", "place": m.group(1).strip(" .!?")}
    if low in ("start", "hi", "hello", "hola", "help", "ayuda", "你好"):
        return {"intent": "help"}
    return {"intent": "ask", "question": t}


HELP_TEXT = (
    "HighGround SF: flood & storm alerts for your block. Text 'alert me for <address or intersection>' "
    "(e.g. 'alert me for 17th and Folsom'). Ask anything, e.g. 'where can I get sandbags?'. Reply STOP anytime. "
    "Not an official alert service; in an emergency call 911."
)


# ---------------------------------------------------------------- triggers -> outbox


def should_alert_risk(sub: dict, cell: str, new: RiskState, now: datetime) -> bool:
    """Pure: alert on reaching the subscriber's minimum; again only if the level rises or the cooldown passed."""
    if sub.get("status") != "active" or new.level.rank < sub.get("min_level_rank", Level.high.rank):
        return False
    last = sub.get("last_alert") or {}
    if not last.get("at"):
        return True
    last_at = datetime.fromisoformat(last["at"])
    if new.level.rank > (last.get("level_rank") or 0):
        return True
    return now - last_at >= RISK_COOLDOWN


def risk_alerts(es: ES, changed: dict[str, RiskState], now: datetime | None = None) -> int:
    """For cells whose level rose this run: find subscribers covering them and enqueue alerts."""
    now = now or datetime.now(UTC)
    if not changed:
        return 0
    res = es.search(
        "subscriptions",
        size=1000,
        query={"bool": {"filter": [{"terms": {"cells": list(changed)}}, {"term": {"status": "active"}}]}},
        _source={"excludes": ["query"]},
    )
    n = 0
    for h in res["hits"]["hits"]:
        sub = h["_source"]
        cell = next((c for c in sub["cells"] if c in changed), None)
        st = changed.get(cell)
        if not st or not should_alert_risk(sub, cell, st, now):
            continue
        window = now.strftime("%Y%m%d") + str(now.hour // 3)
        text = fallback.alert_text(st, sub.get("label") or "your place", sub.get("lang", "en"), f"{settings().base_url}/?cell={cell}")
        n += enqueue(es, h["_id"], sub, kind="risk", key=f"{cell}:{st.level.value}:{window}", text=text, cell=cell, level=st.level, now=now)
    return n


def event_alerts(es: ES, events: list[dict], now: datetime | None = None) -> int:
    """Percolate new hazard events (flood reports, CHP, signals) against subscriptions."""
    now = now or datetime.now(UTC)
    if not events:
        return 0
    docs = [{"ev_location": e["location"], "ev_kind": e["kind"]} for e in events]
    res = es.search(
        "subscriptions",
        size=1000,
        query={"bool": {"filter": [{"percolate": {"field": "query", "documents": docs}}, {"term": {"status": "active"}}]}},
        _source={"excludes": ["query"]},
    )
    n = 0
    for h in res["hits"]["hits"]:
        sub = h["_source"]
        slots = h.get("fields", {}).get("_percolator_document_slot", [0])
        ev = events[slots[0]]
        last = sub.get("last_alert") or {}
        if last.get("at") and now - datetime.fromisoformat(last["at"]) < EVENT_COOLDOWN:
            continue
        what = {"flood_report": "A flooding report", "road_incident": "A CHP road hazard", "street_signal": "An unverified public post about flooding", "tree_report": "A fallen-tree report"}[ev["kind"]]  # fmt: skip
        text = f"HighGround: {what} near {sub.get('label') or 'your place'} ({ev.get('label') or ''}). Avoid the area if you can. Reply STOP to unsubscribe."
        n += enqueue(es, h["_id"], sub, kind="event", key=f"ev:{ev['id']}", text=text[:320], cell=ev.get("cell"), level=None, now=now)
    return n


def enqueue(es: ES, sub_id: str, sub: dict, *, kind: str, key: str, text: str, cell: str | None, level: Level | None, now: datetime) -> int:
    oid = hashlib.sha1(f"{sub_id}|{key}".encode()).hexdigest()
    try:
        es.client.create(
            index=es.ix("alerts-outbox"),
            id=oid,
            document={
                "sub_id": sub_id, "channel": sub["channel"], "kind": kind, "cell": cell, "level": level.value if level else None,
                "payload": text, "lang": sub.get("lang", "en"), "status": "pending", "attempts": 0,
                "next_attempt_at": now.isoformat(), "created_at": now.isoformat(),
            },  # fmt: skip
        )
    except ConflictError:
        return 0  # already queued: idempotent
    es.client.update(
        index=es.ix("subscriptions"),
        id=sub_id,
        doc={"last_alert": {"cell": cell, "level_rank": level.rank if level else (sub.get("last_alert") or {}).get("level_rank"), "at": now.isoformat(), "event_id": key}},
    )  # fmt: skip
    return 1


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(attempts - 1, 0)), 3600))


def dispatch(es: ES, channels: dict[str, Channel], now: datetime | None = None, batch: int = 100) -> dict:
    now = now or datetime.now(UTC)
    res = es.search(
        "alerts-outbox",
        size=batch,
        seq_no_primary_term=True,
        sort=[{"next_attempt_at": "asc"}],
        query={"bool": {"filter": [{"term": {"status": "pending"}}, {"range": {"next_attempt_at": {"lte": now.isoformat()}}}]}},
    )
    stats = {"sent": 0, "retry": 0, "dead": 0, "invalid": 0, "no_channel": 0}
    for h in res["hits"]["hits"]:
        item = h["_source"]
        ch = channels.get(item["channel"])
        try:
            sub = es.client.get(index=es.ix("subscriptions"), id=item["sub_id"])["_source"]
        except NotFoundError:
            _mark(es, h, "cancelled", now)
            continue
        if not ch:
            stats["no_channel"] += 1
            _mark(es, h, "pending", now, attempts=item["attempts"], delay=timedelta(minutes=10), error="channel not configured")
            continue
        handle = decrypt_handle(sub["handle_enc"])
        if not handle:
            _mark(es, h, "dead", now, error="undecryptable handle")
            stats["dead"] += 1
            continue
        handle["idempotency_key"] = h["_id"]
        r = ch.send(handle, item["payload"])
        attempts = item["attempts"] + 1
        if r.ok:
            _mark(es, h, "sent", now, attempts=attempts)
            stats["sent"] += 1
        elif r.invalid_handle:
            _mark(es, h, "dead", now, attempts=attempts, error=r.error)
            es.client.update(index=es.ix("subscriptions"), id=item["sub_id"], doc={"status": "invalid"})
            stats["invalid"] += 1
        elif r.retryable and attempts < MAX_ATTEMPTS:
            _mark(es, h, "pending", now, attempts=attempts, delay=backoff(attempts), error=r.error)
            stats["retry"] += 1
        else:
            _mark(es, h, "dead", now, attempts=attempts, error=r.error)
            stats["dead"] += 1
    return stats


def _mark(es: ES, hit: dict, status: str, now: datetime, attempts: int | None = None, delay: timedelta | None = None, error: str | None = None):
    doc = {"status": status}
    if attempts is not None:
        doc["attempts"] = attempts
    if status == "sent":
        doc["sent_at"] = now.isoformat()
    if delay:
        doc["next_attempt_at"] = (now + delay).isoformat()
    if error:
        doc["error"] = error[:500]
    try:
        es.client.update(index=es.ix("alerts-outbox"), id=hit["_id"], doc=doc, if_seq_no=hit["_seq_no"], if_primary_term=hit["_primary_term"])
    except ConflictError:
        pass  # another dispatcher touched it; it will be re-read next round


def new_id() -> str:
    return uuid.uuid4().hex
