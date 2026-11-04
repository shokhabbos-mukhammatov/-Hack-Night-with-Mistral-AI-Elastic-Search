import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("HG_FERNET_KEY", "x5m9Ya1tXv3cB2nqJ2mZb3t9yH1y0eQGbbK1q1f8K5o=")
os.environ.setdefault("HG_MISTRAL_API_KEY", "")
FIX = ROOT / "tests" / "fixtures"


def load(name: str):
    p = FIX / name
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


@pytest.fixture
def fixtures():
    return load


@pytest.fixture
def now():
    return datetime(2026, 11, 3, 23, 20, tzinfo=UTC)


def _es_url() -> str:
    return os.environ.get("HG_ES_URL", "http://localhost:9200")


@pytest.fixture(scope="session")
def test_es():
    """A real Elasticsearch with a throwaway index prefix; skipped when no cluster is reachable."""
    from elasticsearch import Elasticsearch

    from highground.config import Settings
    from highground.es import ES

    client = Elasticsearch(_es_url(), api_key=os.environ.get("HG_ES_API_KEY") or None, request_timeout=30)
    try:
        client.info()
    except Exception:  # noqa: BLE001
        pytest.skip("no Elasticsearch reachable at HG_ES_URL")
    prefix = f"hgtest-{uuid.uuid4().hex[:6]}-"
    cfg = Settings(es_url=_es_url(), es_api_key=os.environ.get("HG_ES_API_KEY", ""), index_prefix=prefix, mistral_api_key="",
                   fernet_key=os.environ["HG_FERNET_KEY"])  # fmt: skip
    es = ES(cfg, client)
    yield es
    try:
        client.ingest.delete_pipeline(id=f"{prefix}reports-enrich")
    except Exception:  # noqa: BLE001
        pass
    try:
        client.enrich.delete_policy(name=f"{prefix}zones-policy")
    except Exception:  # noqa: BLE001
        pass
    try:
        client.transform.stop_transform(transform_id=f"{prefix}block-hourly", force=True, wait_for_completion=True)
        client.transform.delete_transform(transform_id=f"{prefix}block-hourly", force=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        for ds in client.indices.get_data_stream(name=f"{prefix}*")["data_streams"]:
            client.indices.delete_data_stream(name=ds["name"])
        for t in client.indices.get_index_template(name=f"{prefix}*")["index_templates"]:
            client.indices.delete_index_template(name=t["name"])
    except Exception:  # noqa: BLE001
        pass
    names = [r["index"] for r in client.cat.indices(index=f"{prefix}*", format="json")]
    if names:
        client.indices.delete(index=",".join(names), ignore_unavailable=True)
    for fn, kw in ((client.query_rules.delete_ruleset, {"ruleset_id": f"{prefix}guides-rules"}), (client.synonyms.delete_synonym, {"id": f"{prefix}flood-terms"})):
        try:
            fn(**kw)
        except Exception:  # noqa: BLE001
            pass
