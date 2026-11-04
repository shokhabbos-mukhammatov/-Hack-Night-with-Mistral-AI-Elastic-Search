"""Web Push (VAPID) for the installable PWA: Android and desktop browsers, no phone number needed.

Generate keys once:  python -m highground.messaging.webpush --generate
"""

import json
import sys

from highground.messaging.base import SendResult, classify_http_error


class WebPushChannel:
    name = "webpush"

    def __init__(self, private_key: str, subject: str):
        self.private_key, self.subject = private_key, subject

    def send(self, handle: dict, text: str, url: str | None = None) -> SendResult:
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription_info=handle["subscription"],
                data=json.dumps({"title": "HighGround SF", "body": text, "url": url or "/"}),
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
                timeout=20,
            )
            return SendResult(ok=True)
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            return classify_http_error(status, str(exc))


def generate_keys() -> dict:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return {"HG_VAPID_PRIVATE_KEY": b64(priv), "HG_VAPID_PUBLIC_KEY": b64(pub)}


if __name__ == "__main__" and "--generate" in sys.argv:
    for k, v in generate_keys().items():
        print(f"{k}={v}")
