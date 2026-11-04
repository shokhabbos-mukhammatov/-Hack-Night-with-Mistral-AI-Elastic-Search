"""HighGround SF web service (FastAPI). Request path touches only Elasticsearch, Mistral and the geocoder.

    uvicorn app:app --host 0.0.0.0 --port $PORT
"""

import hmac
import json
import logging
import queue
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from highground import agent, alerts, fallback, ops, report, tools
from highground.briefing import build_briefing
from highground.config import settings
from highground.es import ES
from highground.geo import in_sf
from highground.geocode import geocode
from highground.inbound import handle_text
from highground.messaging.base import available_channels
from highground.messaging.telegram import parse_update
from highground.mistral_client import MistralUnavailable, mistral
from highground.models import Level
from highground.replay import DATA_DIR

log = logging.getLogger("highground.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("elastic_transport").setLevel(logging.WARNING)
WEB = Path(__file__).resolve().parent / "web"
MAX_UPLOAD = 8 * 1024 * 1024

app = FastAPI(title="HighGround SF", version="0.1.0", description="Block-level flood & storm-hazard copilot for San Francisco")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@lru_cache
def es() -> ES:
    return ES()


# ---------------------------------------------------------------- rate limiting + request logs

_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()
LIMITS = {"api": (60, 60), "report": (10, 3600), "subscribe": (5, 3600), "ask": (20, 60)}


def rate_limit(request: Request, bucket: str = "api") -> None:
    n, window = LIMITS[bucket]
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    key, now = f"{bucket}:{ip}", time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            raise HTTPException(429, "Too many requests, please slow down.")
        q.append(now)


_logq: queue.Queue = queue.Queue(maxsize=10_000)


def _log_flusher():
    while True:
        batch = []
        try:
            batch.append(_logq.get(timeout=5))
            while len(batch) < 500:
                batch.append(_logq.get_nowait())
        except queue.Empty:
            pass
        if batch:
            try:
                es().upsert_docs("logs", batch)
            except Exception:  # noqa: BLE001 - logging must never break serving
                pass


threading.Thread(target=_log_flusher, daemon=True).start()


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if not path.startswith("/static") and path not in ("/healthz",):
        try:
            _logq.put_nowait({"_id": f"{time.time_ns()}", "@timestamp": datetime.now(UTC).isoformat(), "event": "http", "route": path,
                              "status": response.status_code, "ms": round(ms, 1), "level": "info"})  # fmt: skip
        except queue.Full:
            pass
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---------------------------------------------------------------- pages + health

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(WEB / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/status")
def status():
    e = es()
    try:
        fresh = tools.latest_freshness(e)
        counts = {name: e.count(name) for name in ("hazard-reports", "risk-cells", "guides", "places", "subscriptions", "alerts-outbox")}
        levels = e.search("risk-cells", size=0, aggs={"l": {"terms": {"field": "level"}}})["aggregations"]["l"]["buckets"]
        es_ok = True
    except Exception as exc:  # noqa: BLE001
        fresh, counts, levels, es_ok = {"error": str(exc)[:200]}, {}, [], False
    return {
        "ok": es_ok,
        "time": datetime.now(UTC).isoformat(),
        "elasticsearch": es_ok,
        "mistral": mistral().enabled,
        "channels": sorted(available_channels()),
        "open311_submit": bool(settings().open311_api_key),
        "feeds": fresh,
        "counts": counts,
        "risk_levels": {b["key"]: b["doc_count"] for b in levels},
    }


# ---------------------------------------------------------------- briefing / ask / geocode


def _point(address: str | None, lat: float | None, lon: float | None) -> tuple[float, float, str | None]:
    if lat is not None and lon is not None:
        if not in_sf(lat, lon):
            raise HTTPException(422, "That location is outside San Francisco.")
        return lat, lon, None
    if not address:
        raise HTTPException(422, "Give an address/intersection or lat & lon.")
    hit = geocode(es(), address)
    if not hit:
        raise HTTPException(404, "Couldn't find that place in San Francisco. Try an intersection like '17th and Folsom'.")
    return hit["lat"], hit["lon"], hit["label"]


@app.get("/api/geocode")
def api_geocode(request: Request, q: str = Query(..., min_length=2, max_length=200)):
    rate_limit(request)
    hit = geocode(es(), q)
    if not hit:
        raise HTTPException(404, "not found")
    return hit


@app.get("/api/briefing")
def api_briefing(request: Request, address: str | None = Query(None, max_length=200), lat: float | None = None, lon: float | None = None, lang: str = "en"):
    rate_limit(request)
    la, lo, label = _point(address, lat, lon)
    if lang == "auto" and address:
        lang = agent.detect_lang(address)
    b = build_briefing(es(), la, lo, lang=lang if lang != "auto" else "en", label=label)
    return JSONResponse(json.loads(b.model_dump_json()), headers={"Cache-Control": "no-store"})


class AskIn(BaseModel):
    question: str = Field(..., min_length=2, max_length=600)
    lang: str | None = "auto"
    lat: float | None = None
    lon: float | None = None
    conversation_id: str | None = Field(None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/api/ask")
def api_ask(request: Request, body: AskIn):
    rate_limit(request, "ask")
    if mistral().enabled:
        try:
            mod = mistral().moderate([body.question])[0]
            if mod["flagged"]:
                raise HTTPException(400, "Let's keep it about flooding, storms and city services.")
        except MistralUnavailable:
            pass
    from highground.mistral_agent import converse

    return converse(es(), body.question, body.lang, body.lat, body.lon, body.conversation_id)


# ---------------------------------------------------------------- report / voice


async def _read_upload(f: UploadFile | None) -> bytes | None:
    if not f:
        return None
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 8 MB).")
    return data or None


@app.post("/api/report")
async def api_report(
    request: Request,
    lat: float = Form(...),
    lon: float = Form(...),
    text: str | None = Form(None, max_length=1000),
    kind: str | None = Form(None),
    photo: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
):
    rate_limit(request, "report")
    if not in_sf(lat, lon):
        raise HTTPException(422, "That location is outside San Francisco.")
    photo_b, audio_b = await _read_upload(photo), await _read_upload(audio)
    if photo and photo.content_type not in ("image/jpeg", "image/png", "image/webp", "image/heic"):
        raise HTTPException(415, "Photo must be JPEG, PNG or WebP.")
    chosen = kind if kind in report.SERVICE else None
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        report.draft_report, es(), lat, lon, photo=photo_b, photo_mime=(photo.content_type if photo else "image/jpeg"), audio=audio_b, text=text, chosen_kind=chosen
    )


class SubmitIn(BaseModel):
    draft: dict


@app.post("/api/report/submit")
def api_report_submit(request: Request, body: SubmitIn):
    rate_limit(request, "report")
    return report.submit(body.draft)


@app.post("/api/voice")
async def api_voice(request: Request, audio: UploadFile = File(...)):
    rate_limit(request, "ask")
    data = await _read_upload(audio)
    if not data:
        raise HTTPException(422, "empty audio")
    try:
        return mistral().transcribe(data, audio.filename or "audio.webm")
    except MistralUnavailable as exc:
        raise HTTPException(503, "Voice input is unavailable right now.") from exc


class SpeakIn(BaseModel):
    text: str = Field(..., max_length=1500)


@app.post("/api/speak")
def api_speak(request: Request, body: SpeakIn):
    rate_limit(request, "ask")
    try:
        audio = mistral().speak(body.text)
    except MistralUnavailable:
        audio = None
    if not audio:
        return Response(status_code=204)  # the page falls back to the browser's speech synthesis
    return Response(content=audio, media_type="audio/mpeg")


# ---------------------------------------------------------------- map layers (ES vector tiles) + text twin

TILE_LAYERS = {
    "reports": ("hazard-reports", "location", ["category", "subcategory", "requested_at", "address", "case_id"]),
    "risk": ("risk-cells", "polygon", ["level", "level_rank", "cell", "score"]),
    "zones": ("flood-zones", "geometry", ["layer"]),
}


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf", include_in_schema=False)
def tiles(layer: str, z: int, x: int, y: int, hours: int = Query(24, ge=1, le=168), zone: str = "100yr"):
    if layer == "density" and 0 <= z <= 20:
        # H3 (geohex) aggregation tiles straight from Elasticsearch: report density without shipping points
        query = {"bool": {"filter": [{"range": {"requested_at": {"gte": f"now-{hours}h"}}}, {"terms": {"category": list(ops.FLOOD) + ["tree"]}}]}}
        try:
            data = es().mvt("hazard-reports", "location", z, x, y, query=query, grid_agg="geohex", grid_precision=6, grid_type="grid", size=0, track_total_hits=False)
        except Exception:  # noqa: BLE001
            return Response(status_code=204)
        return Response(content=data, media_type="application/vnd.mapbox-vector-tile", headers={"Cache-Control": "public, max-age=60"})
    if layer not in TILE_LAYERS or not (0 <= z <= 20):
        raise HTTPException(404)
    index, field, fields = TILE_LAYERS[layer]
    if layer == "reports":
        query = {"bool": {"filter": [{"range": {"requested_at": {"gte": f"now-{hours}h"}}},
                                     {"terms": {"category": ["flooding", "catch_basin", "sewer_backup", "tree", "street"]}}]}}  # fmt: skip
    elif layer == "risk":
        query = {"range": {"level_rank": {"gte": 1}}}
    else:
        query = {"term": {"layer": zone if zone in ("100yr", "slr24") else "100yr"}}
    try:
        data = es().mvt(index, field, z, x, y, query=query, fields=fields, grid_precision=0, exact_bounds=True, size=10000, track_total_hits=False)
    except Exception:  # noqa: BLE001
        return Response(status_code=204)
    return Response(content=data, media_type="application/vnd.mapbox-vector-tile", headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/list/nearby")
def list_nearby(request: Request, lat: float, lon: float, hours: float = Query(6, le=72), radius: int = Query(500, le=2000)):
    """Text twin of the map: every hazard near a point, with distance and time (screen-reader friendly)."""
    rate_limit(request)
    now = datetime.now(UTC)
    ev = tools.nearby_events(es(), lat, lon, now, hours=hours, radius_m=radius)
    return {
        "as_of": now.isoformat(),
        "items": [
            {"kind": e.kind.value, "label": e.label, "distance_m": e.distance_m, "minutes_ago": int((now - e.at).total_seconds() // 60), "source": e.source, "url": e.source_url}
            for e in sorted(ev, key=lambda e: e.distance_m)
        ],
    }


@app.get("/api/list/risk")
def list_risk(request: Request, min_level: Level = Level.elevated, size: int = Query(50, le=500)):
    rate_limit(request)
    res = es().search(
        "risk-cells", size=size, sort=[{"score": "desc"}], query={"range": {"level_rank": {"gte": min_level.rank}}}, _source=["cell", "level", "score", "centroid", "reasons", "as_of"]
    )
    return {"items": [h["_source"] for h in res["hits"]["hits"]]}


# ---------------------------------------------------------------- city ops (ES|QL LOOKUP JOIN, significant_terms, COMPLETION)


@app.get("/ops", include_in_schema=False)
def ops_page():
    return FileResponse(WEB / "ops.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/ops/hotspots")
def api_hotspots(request: Request, hours: int = Query(6, ge=1, le=72), limit: int = Query(20, le=100)):
    rate_limit(request)
    return {"hours": hours, "items": ops.hotspots(es(), hours, limit)}


@app.get("/api/ops/unusual")
def api_unusual(request: Request, hours: int = Query(6, ge=1, le=72)):
    rate_limit(request)
    return ops.unusual(es(), hours=hours)


@app.get("/api/ops/neighborhood/{name}")
def api_neighborhood(request: Request, name: str, hours: int = Query(6, ge=1, le=72)):
    rate_limit(request)
    return ops.neighborhood(es(), name, hours)


@app.get("/api/ops/overview")
def api_ops_overview(request: Request):
    rate_limit(request)
    e = es()
    levels = e.search("risk-cells", size=0, aggs={"l": {"terms": {"field": "level"}}})["aggregations"]["l"]["buckets"]
    hoods = e.search("cell-profiles", size=0, aggs={"n": {"terms": {"field": "neighborhood", "size": 60, "order": {"_key": "asc"}}}})
    return {
        "risk_levels": {b["key"]: b["doc_count"] for b in levels},
        "outbox": ops.outbox_stats(e),
        "feeds": tools.latest_freshness(e),
        "neighborhoods": [b["key"] for b in hoods["aggregations"]["n"]["buckets"]],
    }


# ---------------------------------------------------------------- replay / backtest


@lru_cache(maxsize=16)
def _replay_payload(date: str) -> dict:
    path = DATA_DIR / f"{date}.timeline.json"
    if not path.exists():
        raise HTTPException(404, "No replay for that date.")
    return json.loads(path.read_text())


@app.get("/api/replay")
def replay_index():
    dates = sorted(p.name.split(".")[0] for p in DATA_DIR.glob("*.timeline.json"))
    cal = DATA_DIR / "calibration.json"
    calibration = json.loads(cal.read_text()) if cal.exists() else None
    if calibration:
        calibration.pop("grid", None)
    return {"dates": dates, "calibration": calibration}


@app.get("/api/replay/{date}")
def replay(date: str):
    return JSONResponse(_replay_payload(date), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/backtest/{date}")
def backtest(date: str):
    return _replay_payload(date)["backtest"]


# ---------------------------------------------------------------- subscriptions


class SubscribeIn(BaseModel):
    channel: str = Field(..., pattern="^(webpush|imessage|telegram)$")
    address: str | None = Field(None, max_length=200)
    lat: float | None = None
    lon: float | None = None
    lang: str = "en"
    min_level: Level = Level.high
    push_subscription: dict | None = None
    phone: str | None = Field(None, max_length=20)


@app.get("/api/push/key")
def push_key():
    return {"publicKey": settings().vapid_public_key or None}


@app.post("/api/subscribe")
def api_subscribe(request: Request, body: SubscribeIn):
    rate_limit(request, "subscribe")
    la, lo, label = _point(body.address, body.lat, body.lon)
    label = label or body.address or "your place"
    chans = available_channels()
    if body.channel == "webpush":
        if "webpush" not in chans or not body.push_subscription:
            raise HTTPException(503, "Web Push is not configured on this server.")
        res = alerts.subscribe(es(), "webpush", {"subscription": body.push_subscription}, la, lo, label=label, lang=body.lang, min_level=body.min_level, confirmed=True)
        return {**res, "label": label}
    if body.channel == "telegram":
        if "telegram" not in chans:
            raise HTTPException(503, "Telegram alerts are not configured on this server.")
        username = _telegram_username()
        start = ("alert_" + label).replace(" ", "_")[:60]
        return {"status": "open_link", "url": f"https://t.me/{username}?start={start}", "label": label}
    if body.channel == "imessage":
        ch = chans.get("imessage")
        if not ch or not body.phone:
            raise HTTPException(503, "iMessage alerts are not configured on this server.")
        a = ch.create_assignment(body.phone)
        number = a.get("assignedPhoneNumber")
        msg = f"alert me for {label}"
        return {"status": "text_us", "number": number, "message": msg, "sms_link": f"sms:{number}&body={msg}", "label": label}
    raise HTTPException(422, "unknown channel")


@app.delete("/api/subscribe/{sub_id}")
def api_unsubscribe(sub_id: str, token: str):
    if not alerts.unsubscribe_token(es(), sub_id, token):
        raise HTTPException(404, "Subscription not found.")
    return {"ok": True}


@lru_cache
def _telegram_username() -> str:
    import httpx

    r = httpx.get(f"https://api.telegram.org/bot{settings().telegram_bot_token}/getMe", timeout=10).json()
    return r["result"]["username"]


# ---------------------------------------------------------------- webhooks


@app.post("/webhooks/telegram", include_in_schema=False)
async def telegram_webhook(request: Request, background: BackgroundTasks):
    secret = settings().telegram_webhook_secret
    if not secret or not hmac.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), secret):
        raise HTTPException(403)
    msg = parse_update(await request.json())
    if msg:
        background.add_task(_telegram_reply, msg)
    return {"ok": True}


def _telegram_reply(msg: dict) -> None:
    ch = available_channels().get("telegram")
    if not ch:
        return
    handle = {"chat_id": msg["chat_id"]}
    try:
        reply = handle_text(es(), "telegram", handle, msg["text"], msg.get("lang"))
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram handler failed: %s", exc)
        reply = alerts.HELP_TEXT
    ch.send(handle, reply)


@app.post("/webhooks/apify", include_in_schema=False)
async def apify_webhook(request: Request, secret: str = ""):
    cfg = settings()
    if not cfg.apify_webhook_secret or not hmac.compare_digest(secret, cfg.apify_webhook_secret):
        raise HTTPException(403)
    payload = await request.json()
    run = (payload.get("resource") or {}) if isinstance(payload, dict) else {}
    job_id = f"apify:{run.get('id')}"
    es().client.update(
        index=es().ix("jobs"),
        id=job_id,
        doc={"status": "ready" if run.get("status") == "SUCCEEDED" else "failed", "payload": run, "created_at": datetime.now(UTC).isoformat()},
        doc_as_upsert=True,
    )
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {"languages": fallback.SUPPORTED_LANGS, "disclaimer": fallback.DISCLAIMER, "levels": [lv.value for lv in Level], "generated": (datetime.now(UTC) - timedelta(0)).isoformat()}
