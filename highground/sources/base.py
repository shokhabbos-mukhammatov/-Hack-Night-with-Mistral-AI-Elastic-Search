"""The Source seam and the one shared runner (watermarks, overlap, circuit breaker, idempotent upserts)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx


class Source(Protocol):
    name: str
    index: str  # logical index name (prefix added by the store)
    interval_s: int
    overlap_s: int
    pipeline: str | None

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        """Return docs, each with an "_id". Must be safe to call again with an overlapping window."""
        ...


class Store(Protocol):
    def get_status(self, name: str) -> dict | None: ...
    def save_status(self, name: str, status: dict) -> None: ...
    def upsert_docs(self, index: str, docs: list[dict], pipeline: str | None = None, merge_fn=None) -> int: ...


@dataclass
class RunResult:
    name: str
    ran: bool
    ok: bool = True
    rows: int = 0
    error: str | None = None
    skipped_reason: str | None = None
    extra: dict = field(default_factory=dict)


BREAKER_FAILURES = 3
BREAKER_COOLDOWN = timedelta(minutes=5)


def breaker_open(status: dict | None, now: datetime) -> bool:
    """Pure: after 3 consecutive failures, wait 5 minutes before trying again."""
    if not status or status.get("consecutive_failures", 0) < BREAKER_FAILURES:
        return False
    last = status.get("last_error_at")
    if not last:
        return False
    last_dt = datetime.fromisoformat(last) if isinstance(last, str) else last
    return now - last_dt < BREAKER_COOLDOWN


def next_since(status: dict | None, overlap_s: int) -> datetime | None:
    """Pure: resume from the stored watermark minus an overlap, so late-arriving rows are never missed."""
    if not status or not status.get("watermark"):
        return None
    wm = datetime.fromisoformat(status["watermark"])
    return wm - timedelta(seconds=overlap_s)


def max_timestamp(docs: list[dict], keys=("updated_at", "requested_at", "@timestamp", "published_at")) -> str | None:
    best: str | None = None
    for d in docs:
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and (best is None or v > best):
                best = v
    return best


def run_source(src: Source, http: httpx.Client, store: Store, now: datetime | None = None) -> RunResult:
    now = now or datetime.now(UTC)
    status = store.get_status(src.name) or {}
    if breaker_open(status, now):
        return RunResult(src.name, ran=False, skipped_reason="circuit_open")
    since = next_since(status, src.overlap_s)
    try:
        docs = src.fetch(http, since, now)
        merge_fn = getattr(src, "merge", None)
        rows = store.upsert_docs(src.index, docs, src.pipeline, merge_fn) if docs else 0
        wm = max_timestamp(docs)
        new_status = {
            **status,
            "name": src.name,
            "interval_s": src.interval_s,
            "last_success": now.isoformat(),
            "last_run": now.isoformat(),
            "rows_last_run": rows,
            "consecutive_failures": 0,
            "watermark": max(filter(None, [status.get("watermark"), _iso_utc(wm)]), default=None),
        }
        store.save_status(src.name, new_status)
        return RunResult(src.name, ran=True, ok=True, rows=rows)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised into the scheduler
        new_status = {
            **status,
            "name": src.name,
            "interval_s": src.interval_s,
            "last_run": now.isoformat(),
            "last_error": f"{type(exc).__name__}: {exc}"[:500],
            "last_error_at": now.isoformat(),
            "consecutive_failures": status.get("consecutive_failures", 0) + 1,
        }
        store.save_status(src.name, new_status)
        return RunResult(src.name, ran=True, ok=False, error=new_status["last_error"])


def _iso_utc(ts: str | None) -> str | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()
