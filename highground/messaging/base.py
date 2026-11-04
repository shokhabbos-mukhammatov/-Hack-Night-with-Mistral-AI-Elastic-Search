"""The Channel seam. A handle is channel-specific (Photon space, Telegram chat id, push subscription)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SendResult:
    ok: bool
    retryable: bool = False
    invalid_handle: bool = False
    error: str | None = None


class Channel(Protocol):
    name: str

    def send(self, handle: dict, text: str, url: str | None = None) -> SendResult: ...


def classify_http_error(status: int | None, body: str = "") -> SendResult:
    """Pure: 4xx (except 429) = handle is bad, stop retrying; 429/5xx/network = retry via the outbox."""
    if status is None:
        return SendResult(ok=False, retryable=True, error=body[:300] or "network error")
    if status == 429 or status >= 500:
        return SendResult(ok=False, retryable=True, error=f"{status}: {body[:300]}")
    if status in (400, 403, 404, 410):
        return SendResult(ok=False, invalid_handle=True, error=f"{status}: {body[:300]}")
    return SendResult(ok=False, retryable=False, error=f"{status}: {body[:300]}")


def available_channels() -> dict[str, Channel]:
    from highground.config import settings
    from highground.messaging.photon import PhotonChannel
    from highground.messaging.telegram import TelegramChannel
    from highground.messaging.webpush import WebPushChannel

    cfg = settings()
    out: dict[str, Channel] = {}
    if cfg.photon_api_key and cfg.photon_project_id:
        out["imessage"] = PhotonChannel(cfg.photon_project_id, cfg.photon_api_key)
    if cfg.telegram_bot_token:
        out["telegram"] = TelegramChannel(cfg.telegram_bot_token)
    if cfg.vapid_private_key and cfg.vapid_public_key:
        out["webpush"] = WebPushChannel(cfg.vapid_private_key, cfg.vapid_subject)
    return out
