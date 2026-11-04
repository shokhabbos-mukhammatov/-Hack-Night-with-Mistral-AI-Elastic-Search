"""HighGround worker: every scheduled job + the Photon iMessage listener, guarded by an ES lease lock.

    python worker.py            # run forever (Railway "worker" service)
    python worker.py --once     # run each job once and exit (smoke test / cron-style)
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from highground import alerts, riskloop
from highground import http as http_mod
from highground.config import settings
from highground.es import ES
from highground.ingest import SOURCES
from highground.messaging.base import available_channels
from highground.sources.base import run_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for noisy in ("elastic_transport", "httpx", "apscheduler.executors.default"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("highground.worker")

OWNER = f"{socket.gethostname()}:{os.getpid()}"
LOCK = "worker"
_state = {"last_event_check": datetime.now(UTC) - timedelta(minutes=10), "has_lock": False}


def es() -> ES:
    if "es" not in _state:
        _state["es"] = ES()
    return _state["es"]


LEASE_TTL_S = 90


def renew_lease() -> bool:
    """The only place that touches the lock (every 30 s). Jobs just read the cached result."""
    now = datetime.now(UTC)
    try:
        ok = es().acquire_lock(LOCK, OWNER, ttl_s=LEASE_TTL_S, now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning("lease renewal failed: %s", exc)
        ok = False
    if ok != _state["has_lock"]:
        log.info("lease %s", "acquired" if ok else "lost")
    _state["has_lock"] = ok
    _state["lease_until"] = now + timedelta(seconds=LEASE_TTL_S - 15) if ok else now
    return ok


def leader() -> bool:
    """Only the lease holder does work (safe during overlapping deploys and restarts)."""
    return bool(_state["has_lock"]) and datetime.now(UTC) < _state.get("lease_until", datetime.min.replace(tzinfo=UTC))


def job_source(name: str):
    def run():
        if not leader():
            return
        with http_mod.client() as http:
            r = run_source(SOURCES[name], http, es())
        if not r.ok:
            log.warning("source %s failed: %s", name, r.error)
    run.__name__ = f"source_{name}"
    return run


def job_risk():
    if not leader():
        return
    out = riskloop.run_once(es())
    ev = riskloop.new_events_since(es(), _state["last_event_check"])
    _state["last_event_check"] = datetime.now(UTC)
    out["event_alerts"] = alerts.event_alerts(es(), ev)
    if out["changed"] or out["event_alerts"]:
        log.info("risk loop: %s", out)


def job_dispatch():
    if not leader():
        return
    chans = available_channels()
    if not chans:
        return
    stats = alerts.dispatch(es(), chans)
    if any(stats.values()):
        log.info("dispatch: %s", stats)


def job_nightly():
    if not leader():
        return
    from highground.profiles import build_profiles

    with http_mod.client(timeout=180) as http:
        log.info("datasf sync: %s", run_source(SOURCES["datasf"], http, es()).__dict__)
    log.info("profiles: %s", {k: v for k, v in build_profiles(es()).items() if k != "storm_days"})


def job_signals():
    """During active NWS flood/wind products, collect public posts via Apify (capped) every 15 minutes."""
    cfg = settings()
    if not leader() or not cfg.apify_token or not cfg.has_mistral:
        return
    from highground import tools
    from highground.sources import apify

    active = {a.event for a in tools.active_alerts(es(), datetime.now(UTC))}
    if not active & {"Flood Advisory", "Flood Warning", "Flash Flood Warning", "Flood Watch", "Coastal Flood Advisory", "Coastal Flood Warning", "Wind Advisory", "High Wind Warning"}:
        return
    hook = f"{cfg.base_url}/webhooks/apify?secret={cfg.apify_webhook_secret}" if cfg.apify_webhook_secret else None
    with http_mod.client(timeout=60) as http:
        run = apify.run_actor(http, cfg.apify_token, apify.REDDIT, apify.reddit_input(), min(cfg.apify_max_usd_per_run, 1.0), hook)
    es().client.index(index=es().ix("jobs"), id=f"apify:{run.get('id')}", document={"kind": "signals", "status": "running", "created_at": datetime.now(UTC).isoformat(), "payload": run})


def job_apify_results():
    """Pick up finished Apify runs (marked ready by the webhook, or polled) and ingest their datasets."""
    cfg = settings()
    if not leader() or not cfg.apify_token:
        return
    from highground.guides import ingest_apify_dataset

    res = es().search("jobs", size=10, query={"terms": {"status": ["ready", "running"]}})
    for h in res["hits"]["hits"]:
        job = h["_source"]
        run = job.get("payload") or {}
        if job["status"] == "running":
            with http_mod.client() as http:
                info = http_mod.get_json(http, f"https://api.apify.com/v2/actor-runs/{run.get('id')}", headers={"Authorization": f"Bearer {cfg.apify_token}"})["data"]
            if info["status"] in ("RUNNING", "READY"):
                continue
            run = info
            if info["status"] != "SUCCEEDED":
                es().client.update(index=es().ix("jobs"), id=h["_id"], doc={"status": "failed"})
                continue
        n = ingest_apify_dataset(es(), run.get("defaultDatasetId"), job.get("kind", "guides_crawl"))
        es().client.update(index=es().ix("jobs"), id=h["_id"], doc={"status": "done", "rows": n})
        log.info("apify job %s ingested %s rows", h["_id"], n)


def job_ops():
    """Ops alerts to the team's Telegram chat: stale feeds and dead-lettered alerts."""
    cfg = settings()
    if not leader() or not (cfg.admin_telegram_chat_id and cfg.telegram_bot_token):
        return
    from highground import tools
    from highground.messaging.telegram import TelegramChannel

    stale = [k for k, v in tools.latest_freshness(es()).items() if v["stale"] and k != "datasf"]
    dead = es().client.count(index=es().ix("alerts-outbox"), query={"term": {"status": "dead"}})["count"]
    key = f"{sorted(stale)}|{dead}"
    if (stale or dead) and key != _state.get("ops_key"):
        TelegramChannel(cfg.telegram_bot_token).send({"chat_id": cfg.admin_telegram_chat_id}, f"HighGround ops: stale feeds {stale or 'none'}; dead alerts {dead}")
        _state["ops_key"] = key


def photon_listener():
    cfg = settings()
    if not (cfg.photon_api_key and cfg.photon_project_id):
        return
    from highground.inbound import handle_text
    from highground.messaging.photon import PhotonChannel, listen

    ch = PhotonChannel(cfg.photon_project_id, cfg.photon_api_key)

    def on_message(msg: dict):
        handle = {"space_id": msg["space_id"], "agent_user_id": msg["agent_user_id"]}
        reply = handle_text(es(), "imessage", handle, msg["text"])
        ch.send({**handle, "idempotency_key": f"reply:{msg['message_id']}"}, reply)

    while True:
        try:
            listen(cfg.photon_project_id, cfg.photon_api_key, on_message)
        except Exception as exc:  # noqa: BLE001
            log.warning("photon listener stopped (%s); reconnecting in 10s", exc)
            time.sleep(10)


def register_telegram_webhook():
    cfg = settings()
    if cfg.telegram_bot_token and cfg.telegram_webhook_secret and cfg.base_url.startswith("https://"):
        from highground.messaging.telegram import TelegramChannel

        log.info("telegram webhook: %s", TelegramChannel(cfg.telegram_bot_token).set_webhook(f"{cfg.base_url}/webhooks/telegram", cfg.telegram_webhook_secret))


JOBS = {
    "lease": (renew_lease, 30),
    **{f"source:{n}": (job_source(n), SOURCES[n].interval_s) for n in SOURCES if n != "datasf"},
    "risk": (job_risk, 120),
    "dispatch": (job_dispatch, 15),
    "signals": (job_signals, 900),
    "apify_results": (job_apify_results, 60),
    "ops": (job_ops, 300),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    renew_lease()
    if args.once:
        for name, (fn, _) in JOBS.items():
            t0 = time.time()
            fn()
            log.info("ran %s in %.1fs", name, time.time() - t0)
        es().release_lock(LOCK, OWNER)
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # Railway sends SIGTERM on deploy: release the lease
    register_telegram_webhook()
    threading.Thread(target=photon_listener, daemon=True, name="photon").start()
    sched = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60})
    for name, (fn, every) in JOBS.items():
        sched.add_job(fn, "interval", seconds=every, id=name, next_run_time=datetime.now(UTC))
    sched.add_job(job_nightly, "cron", hour=15, minute=7, id="nightly")  # 07:07 PT (DataSF refreshes ~06:00 PT)
    log.info("worker %s starting %d jobs", OWNER, len(JOBS) + 1)
    try:
        sched.start()
    finally:
        es().release_lock(LOCK, OWNER)


if __name__ == "__main__":
    main()
