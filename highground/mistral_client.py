"""Thin wrapper over the official Mistral SDK: one client, a concurrency cap, timeouts, one retry.

Every method returns None (or raises MistralUnavailable) instead of hanging; callers fall back to templates.
"""

import base64
import json
import logging
import threading
from typing import TypeVar

from pydantic import BaseModel

from highground.config import Settings, settings

log = logging.getLogger("highground.mistral")
T = TypeVar("T", bound=BaseModel)


class MistralUnavailable(RuntimeError):
    pass


class MistralClient:
    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or settings()
        self._sem = threading.BoundedSemaphore(self.cfg.mistral_concurrency)
        self._client = None
        self.available_models: set[str] = set()
        if self.cfg.has_mistral:
            from mistralai.client import Mistral

            self._client = Mistral(api_key=self.cfg.mistral_api_key, timeout_ms=30_000)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def check_models(self) -> dict:
        """GET /v1/models: confirm the configured aliases exist on this account."""
        if not self.enabled:
            return {"enabled": False}
        try:
            res = self._client.models.list()
            ids = set()
            for m in res.data or []:
                ids.add(m.id)
                ids.update(getattr(m, "aliases", None) or [])
            self.available_models = ids
            wanted = {k: getattr(self.cfg, k) for k in ("model_large", "model_small", "model_embed", "model_ocr", "model_moderation", "model_transcribe", "model_tts")}  # fmt: skip
            return {"enabled": True, "missing": {k: v for k, v in wanted.items() if v not in ids}, "count": len(ids)}
        except Exception as exc:  # noqa: BLE001
            log.warning("model list failed: %s", exc)
            return {"enabled": True, "error": str(exc)[:200]}

    def _call(self, fn, *args, timeout_ms: int = 15_000, **kwargs):
        if not self.enabled:
            raise MistralUnavailable("no MISTRAL api key configured")
        acquired = self._sem.acquire(timeout=timeout_ms / 1000)
        if not acquired:
            raise MistralUnavailable("concurrency limit reached")
        try:
            last = None
            for attempt in range(2):
                try:
                    return fn(*args, timeout_ms=timeout_ms, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "raw_response", None), "status_code", None)
                    if attempt == 0 and status in (429, 500, 502, 503, 504):
                        continue
                    break
            raise MistralUnavailable(f"{type(last).__name__}: {last}"[:300])
        finally:
            self._sem.release()

    # ---------- chat ----------
    def chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None, temperature: float = 0.2, timeout_ms: int = 15_000, json_mode: bool = False):
        kwargs = {"model": model or self.cfg.model_large, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self._call(self._client.chat.complete, timeout_ms=timeout_ms, **kwargs)

    def parse(self, messages: list[dict], schema: type[T], *, model: str | None = None, timeout_ms: int = 15_000, temperature: float = 0.1) -> T:
        """Structured output validated against a Pydantic model (JSON schema sent to Mistral)."""
        res = self._call(
            self._client.chat.parse,
            response_format=schema,
            model=model or self.cfg.model_large,
            messages=messages,
            temperature=temperature,
            timeout_ms=timeout_ms,
        )
        msg = res.choices[0].message
        parsed = getattr(msg, "parsed", None)
        if parsed is None:
            parsed = schema.model_validate(json.loads(msg.content))
        return parsed

    def vision_parse(self, prompt: str, image_bytes: bytes | None, schema: type[T], *, image_url: str | None = None, mime: str = "image/jpeg", system: str | None = None) -> T:
        if image_bytes is not None:
            image_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": image_url}]
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        return self.parse(msgs, schema, model=self.cfg.model_large, timeout_ms=20_000)

    # ---------- safety ----------
    def moderate(self, texts: list[str]) -> list[dict]:
        res = self._call(self._client.classifiers.moderate, model=self.cfg.model_moderation, inputs=texts, timeout_ms=8_000)
        out = []
        for r in res.results:
            cats = dict(r.categories or {})
            out.append({"flagged": any(cats.values()), "categories": [k for k, v in cats.items() if v]})
        return out

    # ---------- documents / audio ----------
    def ocr_url(self, url: str) -> list[dict]:
        res = self._call(
            self._client.ocr.process,
            model=self.cfg.model_ocr,
            document={"type": "document_url", "document_url": url},
            timeout_ms=60_000,
        )
        return [{"page": p.index + 1 if hasattr(p, "index") else i + 1, "markdown": p.markdown} for i, p in enumerate(res.pages)]

    def transcribe(self, audio: bytes, filename: str = "audio.webm") -> dict:
        res = self._call(
            self._client.audio.transcriptions.complete,
            model=self.cfg.model_transcribe,
            file={"content": audio, "file_name": filename},
            timeout_ms=30_000,
        )
        return {"text": res.text, "language": getattr(res, "language", None)}

    def speak(self, text: str) -> bytes | None:
        res = self._call(self._client.audio.speech.complete, input=text[:1500], model=self.cfg.model_tts, timeout_ms=30_000)
        data = getattr(res, "audio_data", None)
        if not data:
            return None
        return base64.b64decode(data) if isinstance(data, str) else bytes(data)

    # ---------- batch (pre-generation) ----------
    def batch_chat(self, requests: list[dict], *, model: str | None = None) -> str:
        """Upload a JSONL of chat requests and start a batch job; returns the job id."""
        import io

        lines = "\n".join(json.dumps({"custom_id": r["custom_id"], "body": {"messages": r["messages"], "temperature": 0.2}}) for r in requests)
        f = self._call(
            self._client.files.upload,
            file={"file_name": "highground-batch.jsonl", "content": io.BytesIO(lines.encode())},
            purpose="batch",
            timeout_ms=60_000,
        )
        job = self._call(
            self._client.batch.jobs.create,
            input_files=[f.id],
            model=model or self.cfg.model_small,
            endpoint="/v1/chat/completions",
            timeout_ms=60_000,
        )
        return job.id


_singleton: MistralClient | None = None


def mistral() -> MistralClient:
    global _singleton
    if _singleton is None:
        _singleton = MistralClient()
    return _singleton
