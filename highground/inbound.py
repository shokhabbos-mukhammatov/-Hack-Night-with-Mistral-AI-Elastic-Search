"""Inbound texts (iMessage via Photon, Telegram): subscribe / confirm / stop / ask. Returns the reply text."""

import logging

from highground import agent, alerts, fallback
from highground.briefing import build_briefing
from highground.es import ES
from highground.geocode import geocode
from highground.models import Level

log = logging.getLogger("highground.inbound")

CONFIRM_TEXT = {
    "en": "Got it: {place}. Reply YES to start flood & storm alerts for this block (Reply STOP anytime).",
    "es": "Listo: {place}. Responda SÍ para recibir alertas de inundación y tormenta para esta cuadra (STOP para cancelar).",
    "zh": "已收到：{place}。回覆 YES 開始接收此街區的水浸及暴風雨警報（隨時回覆 STOP 取消）。",
}
ACTIVE_TEXT = {
    "en": "Alerts are on. You'll get a text if flood risk near your place rises to High. Reply STOP to unsubscribe.",
    "es": "Alertas activadas. Le avisaremos si el riesgo de inundación cerca de su lugar sube a Alto. STOP para cancelar.",
    "zh": "警報已開啟。如附近水浸風險升至「高」，我們會通知你。回覆 STOP 取消。",
}
STOPPED_TEXT = {"en": "You're unsubscribed. No more alerts.", "es": "Canceló su suscripción. No habrá más alertas.", "zh": "已取消訂閱，不會再收到警報。"}
NOT_FOUND = {
    "en": "I couldn't find that place in San Francisco. Try an intersection like '17th and Folsom' or a street address.",
    "es": "No encontré ese lugar en San Francisco. Pruebe un cruce como '17th y Folsom' o una dirección.",
    "zh": "在三藩市找不到這個地點。請試試路口，例如「17th and Folsom」，或街道地址。",
}


def _t(table: dict, lang: str, **kw) -> str:
    return table.get(lang, table["en"]).format(**kw)


def handle_text(es: ES, channel: str, handle: dict, text: str, lang: str | None = None) -> str:
    lang = lang if lang in ("en", "es", "zh", "tl", "vi", "ru") else agent.detect_lang(text)
    intent = alerts.parse_intent(text)
    key = alerts.handle_key(channel, handle)
    kind = intent["intent"]
    if kind == "stop":
        alerts.set_status_for_handle(es, channel, key, "stopped")
        return _t(STOPPED_TEXT, lang)
    if kind == "confirm":
        n = alerts.set_status_for_handle(es, channel, key, "active")
        return _t(ACTIVE_TEXT, lang) if n else fallback.headline(Level.low, lang) + " " + alerts.HELP_TEXT
    if kind == "help":
        return alerts.HELP_TEXT
    if kind == "subscribe":
        hit = geocode(es, intent["place"])
        if not hit:
            return _t(NOT_FOUND, lang)
        alerts.subscribe(es, channel, handle, hit["lat"], hit["lon"], label=hit["label"], lang=lang)
        b = build_briefing(es, hit["lat"], hit["lon"], lang=lang, label=hit["label"], narrate_with_llm=False)
        return _t(CONFIRM_TEXT, lang, place=hit["label"]) + "\n\n" + fallback.headline(b.risk.level, lang)
    from highground.mistral_agent import converse, get_thread, put_thread, thread_key

    tkey = thread_key(channel, key)
    res = converse(es, intent.get("question", text), lang=lang, conversation_id=get_thread(es, tkey))
    if res.get("conversation_id"):
        put_thread(es, tkey, res["conversation_id"])
    reply = res["answer"]
    if res.get("citations"):
        reply += "\n" + res["citations"][0]["url"]
    return reply[:1500]
