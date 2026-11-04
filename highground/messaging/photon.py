"""iMessage via Photon (Python port of the NightHacks src/photon.ts: HTTP API out, Socket.IO events in)."""

import logging
import uuid
from collections.abc import Callable

import httpx

from highground.messaging.base import SendResult, classify_http_error

API = "https://api.photon.codes"
log = logging.getLogger("highground.photon")


class PhotonChannel:
    name = "imessage"

    def __init__(self, project_id: str, api_key: str):
        self.project_id, self.api_key = project_id, api_key

    def _headers(self, idem: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        h["Idempotency-Key"] = idem or str(uuid.uuid4())
        return h

    def send(self, handle: dict, text: str, url: str | None = None) -> SendResult:
        body = {
            "spaceId": handle["space_id"],
            "senderId": handle["agent_user_id"],
            "content": {"type": "text", "format": "plain", "text": (text + (f"\n{url}" if url else ""))[:3500]},
        }
        try:
            r = httpx.post(
                f"{API}/v1/projects/{self.project_id}/messages", json=body, headers=self._headers(handle.get("idempotency_key")), timeout=20
            )
        except httpx.HTTPError as exc:
            return classify_http_error(None, str(exc))
        return SendResult(ok=True) if r.is_success else classify_http_error(r.status_code, r.text)

    def create_assignment(self, phone: str) -> dict:
        """Assign a shared iMessage line to a phone number; returns the number the resident should text."""
        r = httpx.post(
            f"{API}/v1/projects/{self.project_id}/platforms/imessage/assignments",
            json={"handle": phone},
            headers=self._headers(f"join:{phone.lower()}"),
            timeout=20,
        )
        if r.is_success:
            return r.json()
        if r.status_code == 409:
            lst = httpx.get(
                f"{API}/v1/projects/{self.project_id}/platforms/imessage/assignments",
                params={"pageSize": 200},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20,
            ).json()
            items = lst.get("assignments") or (lst.get("data") or {}).get("assignments") or []
            for a in items:
                if (a.get("handle") or "").lower() == phone.lower():
                    return a
        r.raise_for_status()
        return {}


def normalize_event(event: dict) -> dict | None:
    """Pure: Photon delivery event -> {space_id, agent_user_id, sender_handle, text, message_id} or None."""
    if (event or {}).get("type") != "message.received":
        return None
    m = (event.get("data") or {}).get("object") or {}
    if m.get("platform") != "imessage" or m.get("status") != "received":
        return None
    texts: list[str] = []

    def collect(c):
        if not isinstance(c, dict):
            return
        if c.get("type") == "text" and isinstance(c.get("text"), str):
            texts.append(c["text"])
        elif c.get("type") == "group":
            for p in c.get("parts") or []:
                collect(p)

    collect(m.get("content"))
    agent = (m.get("receivedBy") or {}).get("id") or (m.get("addressedTo") or {}).get("id")
    space = (m.get("space") or {}).get("id")
    sender = m.get("sentBy") or {}
    if not agent or not space or not sender.get("id"):
        return None
    return {
        "message_id": m.get("id"),
        "space_id": space,
        "agent_user_id": agent,
        "sender_handle": sender.get("handle") or sender.get("id"),
        "text": "\n".join(texts).strip(),
    }


def listen(project_id: str, api_key: str, on_message: Callable[[dict], None]):
    """Blocking Socket.IO listener (runs in the worker). Acks deliveries and de-duplicates by event id."""
    import socketio

    sio = socketio.Client(reconnection=True, reconnection_delay=2, reconnection_delay_max=30)
    state = {"cursor": 0, "seen": set()}

    @sio.event
    def connect():
        def ack(result):
            log.info("photon subscribed: %s", str(result)[:200])

        sio.emit("subscribe", {"startSeq": state["cursor"]}, callback=ack)

    @sio.on("delivery")
    def delivery(event, meta=None):
        meta = meta or {}
        if isinstance(meta.get("seq"), int):
            state["cursor"] = max(state["cursor"], meta["seq"])
        eid = (event or {}).get("id")
        if eid in state["seen"]:
            return {"eventId": eid, "status": 200}
        state["seen"].add(eid)
        msg = normalize_event(event)
        if msg:
            try:
                on_message(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("inbound handler failed: %s", exc)
        if meta.get("replyExpected"):
            sio.emit("reply", {"eventId": eid, "status": 200})

    sio.connect(
        API,
        socketio_path=f"/v1/projects/{project_id}/events/ws",
        transports=["websocket"],
        headers={"Authorization": f"Bearer {api_key}"},
    )
    sio.wait()
