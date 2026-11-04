from datetime import UTC, datetime, timedelta

from highground.sources.base import breaker_open, next_since, run_source


class FakeStore:
    def __init__(self):
        self.status, self.docs = {}, []

    def get_status(self, name):
        return self.status.get(name)

    def save_status(self, name, status):
        self.status[name] = status

    def upsert_docs(self, index, docs, pipeline=None, merge_fn=None):
        self.docs.extend(docs)
        return len(docs)


class FakeSource:
    name, index, interval_s, overlap_s, pipeline = "fake", "x", 60, 600, None

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def fetch(self, http, since, now):
        self.calls.append(since)
        if self.fail:
            raise RuntimeError("upstream down")
        return [{"_id": "a", "requested_at": "2026-11-03T10:00:00+00:00"}]


def test_watermark_and_overlap():
    store, src, now = FakeStore(), FakeSource(), datetime(2026, 11, 3, 12, tzinfo=UTC)
    r = run_source(src, None, store, now)
    assert r.ok and r.rows == 1 and src.calls == [None]
    assert store.status["fake"]["watermark"] == "2026-11-03T10:00:00+00:00"
    run_source(src, None, store, now)
    assert src.calls[1] == datetime(2026, 11, 3, 9, 50, tzinfo=UTC), "resume 10 min before the watermark"


def test_circuit_breaker_opens_after_three_failures_and_cools_down():
    store, src, now = FakeStore(), FakeSource(fail=True), datetime(2026, 11, 3, 12, tzinfo=UTC)
    for _ in range(3):
        assert not run_source(src, None, store, now).ok
    assert store.status["fake"]["consecutive_failures"] == 3
    skipped = run_source(src, None, store, now + timedelta(minutes=1))
    assert not skipped.ran and skipped.skipped_reason == "circuit_open"
    assert not breaker_open(store.status["fake"], now + timedelta(minutes=6))
    src.fail = False
    assert run_source(src, None, store, now + timedelta(minutes=6)).ok
    assert store.status["fake"]["consecutive_failures"] == 0


def test_next_since_none_without_status():
    assert next_since(None, 60) is None
