"""Official guidance corpus: load (Apify crawl or curated local files, PDFs via Mistral OCR) and hybrid search.

Search = Elasticsearch retrievers: RRF over BM25 and semantic_text (Mistral embeddings through the inference
endpoint). Without the semantic field it degrades to BM25 only.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from highground import http as http_mod
from highground.config import settings
from highground.es import ES
from highground.mistral_client import MistralUnavailable, mistral
from highground.sources import apify

log = logging.getLogger("highground.guides")
LOCAL_DIR = Path(__file__).resolve().parent.parent / "data" / "guides"
PDFS = {
    # official SFPUC PDFs (checked 2026-11-03), extracted with Mistral OCR when a key is configured
    "https://www.sfpuc.gov/sites/default/files/learning/RainReadySF/FloodInsuranceGuide_multi-lang_08-24-2023_FINAL.pdf": "multi",
}


def local_docs(now: datetime | None = None) -> list[dict]:
    """data/guides/*.md: front matter (url, lang, title) + markdown sections."""
    now = now or datetime.now(UTC)
    docs = []
    for path in sorted(LOCAL_DIR.glob("*.md")):
        raw = path.read_text()
        meta, body = {}, raw
        if raw.startswith("---"):
            head, body = raw[3:].split("---", 1)
            for line in head.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        for i, section in enumerate(s for s in re.split(r"\n(?=## )", body.strip()) if s.strip()):
            title = section.split("\n", 1)[0].strip("# ").strip()
            docs.append(
                {
                    "_id": f"local:{path.stem}#{i}",
                    "url": meta.get("url"),
                    "lang": meta.get("lang", "en"),
                    "title": f"{meta.get('title', path.stem)}: {title}",
                    "section": title,
                    "body": section,
                    "content": section,
                    "source_type": "curated",
                    "fetched_at": now.isoformat(),
                }
            )
    return docs


def ocr_docs() -> list[dict]:
    m = mistral()
    if not m.enabled:
        return []
    docs = []
    for url, lang in PDFS.items():
        try:
            pages = m.ocr_url(url)
        except MistralUnavailable as exc:
            log.warning("OCR failed for %s: %s", url, exc)
            continue
        for p in pages:
            text = (p["markdown"] or "").strip()
            if len(text) < 120:
                continue
            docs.append(
                {
                    "_id": f"pdf:{url}#p{p['page']}",
                    "url": url,
                    "lang": lang,
                    "title": f"{url.rsplit('/', 1)[-1]} (page {p['page']})",
                    "section": f"page {p['page']}",
                    "body": text[:6000],
                    "content": text[:6000],
                    "source_type": "pdf",
                    "page": p["page"],
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            )
    return docs


EMERGENCY_SECTIONS = ("Report flooding", "Reportar inundaciones", "報告水浸", "Flooded streets", "Calles inundadas", "水浸街道")


def load_guides(es: ES, local_only: bool = False) -> dict:
    from highground.es import ensure_query_rules

    docs = local_docs()
    out = {"local": es.upsert_docs("guides", docs, refresh="wait_for")}
    pinned: dict[str, list[str]] = {}
    for d in docs:
        if d["section"].startswith(EMERGENCY_SECTIONS):
            pinned.setdefault(d["lang"], []).append(d["_id"])
    out["query_rules"] = ensure_query_rules(es, pinned) if pinned else False
    out["pdf"] = es.upsert_docs("guides", ocr_docs())
    cfg = settings()
    if not local_only and cfg.apify_token:
        with http_mod.client(timeout=60) as http:
            run = apify.run_actor(http, cfg.apify_token, apify.CRAWLER, apify.crawler_input(), cfg.apify_max_usd_per_run)
        out["apify_run"] = {"id": run.get("id"), "status": run.get("status"), "dataset": run.get("defaultDatasetId")}
        es.client.index(index=es.ix("jobs"), id=f"apify:{run.get('id')}", document={"kind": "guides_crawl", "status": "running", "created_at": datetime.now(UTC).isoformat(), "payload": run})  # fmt: skip
    return out


def ingest_apify_dataset(es: ES, dataset_id: str, kind: str) -> int:
    cfg = settings()
    with http_mod.client(timeout=120) as http:
        items = apify.dataset_items(http, cfg.apify_token, dataset_id)
    if kind == "guides_crawl":
        return es.upsert_docs("guides", apify.guide_docs(items))
    if kind == "signals":
        from highground.signals import process_candidates

        return process_candidates(es, apify.signal_candidates(items))
    return 0


def q_search(query: str, lang: str, k: int, semantic: bool, rerank_id: str | None = None, ruleset_id: str | None = None) -> dict:
    """Retriever tree: [query rules] -> [Elastic Rerank] -> RRF(BM25 with multilingual synonyms, semantic_text)."""
    lexical = {
        "standard": {
            "query": {
                "bool": {
                    "must": {"multi_match": {"query": query, "fields": ["title^2", "section^2", "body"]}},
                    # strong preference for the reader's language; synonyms still bridge languages when needed
                    "should": [{"term": {"lang": {"value": lang, "boost": 25}}}],
                }
            }
        }
    }
    src = ["url", "title", "body", "lang", "source_type", "page"]
    if semantic:
        retriever = {
            "rrf": {
                "retrievers": [lexical, {"standard": {"query": {"semantic": {"field": "content", "query": query}}}}],
                "rank_window_size": 50,
                "rank_constant": 20,
            }
        }
    else:
        retriever = lexical
    if rerank_id:
        retriever = {
            "text_similarity_reranker": {
                "retriever": retriever, "field": "body", "inference_id": rerank_id, "inference_text": query, "rank_window_size": 20,
            }
        }  # fmt: skip
    if ruleset_id:
        retriever = {"rule": {"retriever": retriever, "ruleset_ids": [ruleset_id], "match_criteria": {"query_string": query, "lang": lang}}}
    return {"size": k, "retriever": retriever, "_source": src}


_semantic: bool | None = None
_features: dict | None = None


def search_features(es: ES) -> dict:
    """Which optional retrieval layers work on this cluster (checked once per process)."""
    global _features
    if _features is None:
        from highground.es import QUERY_RULESET, RERANK_ENDPOINT, inference_works

        rules = False
        try:
            es.client.query_rules.get_ruleset(ruleset_id=es.ix(QUERY_RULESET))
            rules = True
        except Exception:  # noqa: BLE001
            pass
        _features = {
            "semantic": semantic_enabled(es),
            "rerank": RERANK_ENDPOINT if inference_works(es, RERANK_ENDPOINT, "rerank") else None,
            "rules": es.ix(QUERY_RULESET) if rules else None,
        }
    return _features


def semantic_enabled(es: ES) -> bool:
    global _semantic
    if _semantic is None:
        try:
            m = es.client.indices.get_mapping(index=es.ix("guides"))
            props = next(iter(m.body.values()))["mappings"]["properties"]
            _semantic = props.get("content", {}).get("type") == "semantic_text"
        except Exception:  # noqa: BLE001
            _semantic = False
    return _semantic


def search(es: ES, query: str, lang: str = "en", k: int = 4) -> list[dict]:
    if not query.strip():
        return []
    f = search_features(es)
    try:
        res = es.search("guides", **q_search(query, lang, k, f["semantic"], f["rerank"], f["rules"]))
    except Exception as exc:  # noqa: BLE001 - any optional layer failing -> plain BM25 + synonyms
        log.info("guide search fallback to BM25: %s", exc)
        res = es.search("guides", **q_search(query, lang, k, False))
    return [
        {"url": h["_source"].get("url"), "title": h["_source"].get("title"), "text": h["_source"].get("body", "")[:1500], "lang": h["_source"].get("lang")}
        for h in res["hits"]["hits"]
    ]
