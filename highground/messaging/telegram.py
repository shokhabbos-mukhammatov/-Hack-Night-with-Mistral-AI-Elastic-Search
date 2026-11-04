"""Telegram bot: works on any phone, instant to set up (@BotFather), inbound via webhook."""

import httpx

from highground.messaging.base import SendResult, classify_http_error


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, handle: dict, text: str, url: str | None = None) -> SendResult:
        payload = {"chat_id": handle["chat_id"], "text": (text + (f"\n{url}" if url else ""))[:4000], "disable_web_page_preview": True}
        try:
            r = httpx.post(f"{self.base}/sendMessage", json=payload, timeout=20)
        except httpx.HTTPError as exc:
            return classify_http_error(None, str(exc))
        if r.is_success and r.json().get("ok"):
            return SendResult(ok=True)
        return classify_http_error(r.status_code, r.text)

    def set_webhook(self, url: str, secret: str) -> dict:
        r = httpx.post(f"{self.base}/setWebhook", json={"url": url, "secret_token": secret, "allowed_updates": ["message"]}, timeout=20)
        return r.json()


def parse_update(update: dict) -> dict | None:
    """Pure: Telegram update -> {chat_id, text, lang} (text messages and /start payloads only)."""
    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    text = msg.get("text")
    if not chat.get("id") or not isinstance(text, str):
        return None
    if text.startswith("/start"):
        payload = text[len("/start") :].strip()
        text = payload.replace("_", " ") if payload else "start"
    return {"chat_id": chat["id"], "text": text.strip(), "lang": ((msg.get("from") or {}).get("language_code") or "en").split("-")[0]}
