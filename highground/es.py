"""Elasticsearch: the only datastore. Mappings, setup (inference, enrich, pipeline, transform), and the Store.

Works against Elastic Cloud Serverless (no shard/replica settings are set) and a local single node.
"""

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from elasticsearch import ApiError, ConflictError, Elasticsearch, NotFoundError
from elasticsearch.helpers import BulkIndexError, bulk

from highground.config import Settings, settings

log = logging.getLogger("highground.es")

GEO = {"type": "geo_point"}
KW = {"type": "keyword"}
DATE = {"type": "date"}
TEXT = {"type": "text"}
FLOAT = {"type": "float"}
BOOL = {"type": "boolean"}
NOINDEX = {"type": "keyword", "index": False, "doc_values": False}

MAPPINGS: dict[str, dict] = {
    "hazard-reports": {
        "properties": {
            "case_id": KW, "category": KW, "subcategory": KW, "category_source": KW, "service_name": KW,
            "service_code": KW, "service_details": KW, "on_street": BOOL, "status": KW, "status_notes": TEXT,
            "requested_at": DATE, "updated_at": DATE, "closed_at": DATE, "ingested_at": DATE,
            "location": GEO, "h3_9": KW, "address": {"type": "text", "fields": {"kw": KW}},
            "neighborhood": KW, "description": TEXT, "media_url": NOINDEX, "reporting_channel": KW,
            "seen_open311": BOOL, "seen_datasf": BOOL, "zone_100yr": BOOL, "slr24": BOOL,
            "vision": {"properties": {"water_visible": BOOL, "confidence": FLOAT, "checked_at": DATE, "model": KW, "depth": KW, "drain_blocked": BOOL}},
            "llm_checked_at": DATE, "llm_confidence": FLOAT,
        }
    },
    "flood-zones": {
        "properties": {
            "layer": KW, "name": KW, "dataset_id": KW, "source_url": NOINDEX, "class": KW,
            "geometry": {"type": "geo_shape"},
        }
    },
    "conditions": {
        "properties": {
            "@timestamp": DATE, "kind": KW, "point": KW, "rain_mm": FLOAT, "gust_kmh": FLOAT, "is_forecast": BOOL,
            "value": FLOAT, "unit": KW, "tide_type": KW, "event": KW, "severity": KW, "headline": TEXT,
            "description": TEXT, "area": TEXT, "valid_from": DATE, "valid_to": DATE, "issued_at": DATE,
            "ugc": KW, "url": NOINDEX, "source": KW, "fetched_at": DATE, "replay": KW,
        }
    },
    "road-incidents": {
        "properties": {
            "log_id": KW, "type": KW, "hazard": KW, "location": GEO, "h3_9": KW, "location_desc": TEXT,
            "area": KW, "details": TEXT, "started_at": DATE, "@timestamp": DATE, "last_seen": DATE, "source": KW,
        }
    },
    "street-signals": {
        "properties": {
            "source": KW, "url": NOINDEX, "published_at": DATE, "excerpt": TEXT, "location": GEO, "h3_9": KW,
            "hazard_type": KW, "confidence": FLOAT, "place_text": TEXT, "is_hazard": BOOL, "ingested_at": DATE,
            "replay": KW,
        }
    },
    "guides": {
        "properties": {
            "url": KW, "lang": KW, "title": {"type": "text", "analyzer": "hg_index", "search_analyzer": "hg_search"},
            "section": {"type": "text", "analyzer": "hg_index", "search_analyzer": "hg_search"},
            "body": {"type": "text", "analyzer": "hg_index", "search_analyzer": "hg_search"}, "source_type": KW, "page": {"type": "integer"},
            "fetched_at": DATE,
            # "content" becomes semantic_text (Mistral embeddings via the inference endpoint) when available
            "content": TEXT,
        }
    },
    "places": {
        "properties": {
            "kind": KW, "name": {"type": "search_as_you_type"}, "alt": {"type": "search_as_you_type"},
            "location": GEO, "h3_9": KW, "neighborhood": KW, "zip": KW,
        }
    },
    "cell-profiles": {
        "properties": {
            "cell": KW, "h3_9": KW, "in_100yr": BOOL, "slr24": BOOL, "shoreline": BOOL, "reports_5y": {"type": "integer"},
            "storm_reports_5y": {"type": "integer"}, "storm_days_5y": {"type": "integer"}, "top_dates": KW,
            "centroid": GEO, "neighborhood": KW, "as_of": DATE,
        }
    },
    "risk-cells": {
        "properties": {
            "cell": KW, "level": KW, "level_rank": {"type": "byte"}, "score": FLOAT,
            "components": {"properties": {"susceptibility": FLOAT, "hazard": FLOAT, "confirmation": FLOAT, "trees": FLOAT}},
            "reasons": {"type": "object", "enabled": False},
            "as_of": DATE, "inputs_hash": KW, "centroid": GEO, "polygon": {"type": "geo_shape"},
        }
    },
    "risk-history": {
        "properties": {"@timestamp": DATE, "cell": KW, "level": KW, "level_rank": {"type": "byte"}, "score": FLOAT, "replay": KW}
    },
    "block-hourly": {
        "properties": {"h3_9": KW, "hour": DATE, "total": {"type": "long"}, "flooding": {"type": "long"}, "tree": {"type": "long"}}
    },
    "subscriptions": {
        "properties": {
            "channel": KW, "handle_enc": NOINDEX, "handle_hash": KW, "lang": KW, "label": TEXT, "location": GEO,
            "radius_m": {"type": "integer"}, "cells": KW, "min_level": KW, "min_level_rank": {"type": "byte"},
            "status": KW, "created_at": DATE, "confirmed_at": DATE, "unsub_token_hash": KW,
            "last_alert": {"properties": {"cell": KW, "level_rank": {"type": "byte"}, "at": DATE, "event_id": KW}},
            "push": {"type": "object", "enabled": False},
            # percolator: each subscription stores the query that matches hazard events near it
            "query": {"type": "percolator"},
            "ev_location": GEO, "ev_kind": KW,
        }
    },
    "alerts-outbox": {
        "properties": {
            "sub_id": KW, "channel": KW, "kind": KW, "cell": KW, "level": KW, "payload": NOINDEX, "lang": KW,
            "status": KW, "attempts": {"type": "integer"}, "next_attempt_at": DATE, "created_at": DATE,
            "sent_at": DATE, "error": TEXT,
        }
    },
    "narratives": {
        "properties": {"cell": KW, "lang": KW, "state_hash": KW, "text": NOINDEX, "created_at": DATE, "expires_at": DATE}
    },
    "geocode-cache": {"properties": {"query": KW, "lat": FLOAT, "lon": FLOAT, "label": TEXT, "source": KW, "created_at": DATE}},
    "feed-status": {"properties": {"name": KW, "last_success": DATE, "last_run": DATE, "last_error_at": DATE, "watermark": DATE}},
    "locks": {"properties": {"owner": KW, "expires_at": DATE}},
    "jobs": {"properties": {"kind": KW, "status": KW, "created_at": DATE, "payload": {"type": "object", "enabled": False}}},
    "backtests": {"properties": {"date": KW, "created_at": DATE, "result": {"type": "object", "enabled": False}}},
    "logs": {"properties": {"@timestamp": DATE, "event": KW, "route": KW, "status": {"type": "short"}, "ms": FLOAT, "level": KW}},
}  # fmt: skip

PIPELINE = "reports-enrich"
ENRICH_POLICY = "zones-policy"
TRANSFORM = "block-hourly"
EMBED_ENDPOINT = "mistral-embed"
CHAT_ENDPOINT = "mistral-chat"  # chat_completion: Kibana/Agent Builder can use Mistral through it
COMPLETION_ENDPOINT = "mistral-completion"  # completion: ES|QL COMPLETION
E5_FALLBACK = ".multilingual-e5-small-elasticsearch"  # Elastic-hosted multilingual embeddings (needs ML / EIS)
RERANK_ENDPOINT = ".rerank-v1-elasticsearch"  # Elastic Rerank
SYNONYMS_SET = "flood-terms"
QUERY_RULESET = "guides-rules"
DATA_STREAMS = {"logs": "30d", "risk-history": "400d"}  # append-only time series with lifecycle retention
INDEX_SETTINGS = {"cell-profiles": {"index.mode": "lookup"}}  # joinable from ES|QL (LOOKUP JOIN)

# Multilingual flood vocabulary (en / es / zh / tl / vi): lexical search matches across languages even
# without embeddings. Updateable in place via the Synonyms API.
SYNONYMS = [
    "sandbag, sandbags, sand bag, sacos de arena, saco de arena, 沙包, sako ng buhangin, bao cát",
    "storm drain, catch basin, drain, desagüe, desague, alcantarilla, coladera, 雨水渠, 渠, kanal, cống thoát nước",
    "flood, flooding, flooded, inundación, inundacion, inundaciones, inundado, 水浸, 水淹, baha, ngập, ngập lụt",
    "flood insurance, seguro contra inundaciones, 水災保險, insurance ng baha, bảo hiểm lũ lụt",
    "fallen tree, tree down, árbol caído, arbol caido, 塌樹, 倒樹, natumbang puno, cây đổ",
    "alert, alerts, alerta, alertas, 警報, babala, cảnh báo",
    "grant, subvención, subvencion, 資助, tulong, trợ cấp",
    "report, reportar, 報告, iulat, báo cáo",
]


class ES:
    """Thin wrapper: one client, prefixed index names, and the Store protocol used by run_source()."""

    def __init__(self, cfg: Settings | None = None, client: Elasticsearch | None = None):
        self.cfg = cfg or settings()
        if client is not None:
            self.client = client
        elif self.cfg.es_api_key:
            self.client = Elasticsearch(self.cfg.es_url, api_key=self.cfg.es_api_key, request_timeout=30)
        else:
            self.client = Elasticsearch(self.cfg.es_url, request_timeout=30)
        self._pipeline_ok: bool | None = None

    def ix(self, name: str) -> str:
        return self.cfg.index(name)

    # ---------- Store protocol ----------
    def get_status(self, name: str) -> dict | None:
        try:
            return self.client.get(index=self.ix("feed-status"), id=name)["_source"]
        except NotFoundError:
            return None

    def save_status(self, name: str, status: dict) -> None:
        self.client.index(index=self.ix("feed-status"), id=name, document=status)

    def upsert_docs(
        self,
        index: str,
        docs: list[dict],
        pipeline: str | None = None,
        merge_fn: Callable[[dict | None, dict], dict] | None = None,
        refresh: bool | str = False,
    ) -> int:
        if not docs:
            return 0
        if merge_fn:
            docs = self._merge_existing(index, docs, merge_fn)
        use_pipeline = self.ix(pipeline) if pipeline and self.pipeline_available() else None
        op = "create" if index in DATA_STREAMS else "index"
        actions = []
        for d in docs:
            src = {k: v for k, v in d.items() if k != "_id"}
            actions.append({"_op_type": op, "_index": self.ix(index), "_id": d["_id"], "_source": src})
        try:
            ok, _ = bulk(self.client.options(request_timeout=120), actions, pipeline=use_pipeline, refresh=refresh, chunk_size=1000)
        except BulkIndexError as exc:
            reasons = {((e.get("index") or {}).get("error") or {}).get("type", "?") for e in exc.errors}
            log.warning("bulk: %d of %d docs rejected on %s (%s)", len(exc.errors), len(actions), index, ", ".join(sorted(reasons)))
            ok = len(actions) - len(exc.errors)
        return ok

    def _merge_existing(self, index: str, docs: list[dict], merge_fn) -> list[dict]:
        # collapse duplicates within the batch first (same case seen twice)
        by_id: dict[str, dict] = {}
        for d in docs:
            by_id[d["_id"]] = merge_fn(by_id.get(d["_id"]), d)
        existing: dict[str, dict] = {}
        ids = list(by_id)
        for i in range(0, len(ids), 1000):
            try:
                res = self.client.mget(index=self.ix(index), ids=ids[i : i + 1000])
            except NotFoundError:
                continue
            for d in res["docs"]:
                if d.get("found"):
                    existing[d["_id"]] = d["_source"]
        out = []
        for _id, d in by_id.items():
            merged = merge_fn(existing.get(_id), d)
            merged["_id"] = _id
            out.append(merged)
        return out

    def pipeline_available(self) -> bool:
        if self._pipeline_ok is None:
            try:
                self.client.ingest.get_pipeline(id=self.ix(PIPELINE))
                self._pipeline_ok = True
            except NotFoundError:
                self._pipeline_ok = False
        return self._pipeline_ok

    # ---------- locks (worker lease) ----------
    def acquire_lock(self, name: str, owner: str, ttl_s: int = 120, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        idx, doc = self.ix("locks"), {"owner": owner, "expires_at": (now + timedelta(seconds=ttl_s)).isoformat()}
        try:
            self.client.create(index=idx, id=name, document=doc, refresh="wait_for")
            return True
        except ConflictError:
            pass
        cur = self.client.get(index=idx, id=name)
        src = cur["_source"]
        if src["owner"] == owner or datetime.fromisoformat(src["expires_at"]) < now:
            try:
                self.client.index(
                    index=idx, id=name, document=doc, if_seq_no=cur["_seq_no"], if_primary_term=cur["_primary_term"], refresh="wait_for"
                )
                return True
            except ConflictError:
                return False
        return False

    def release_lock(self, name: str, owner: str) -> None:
        try:
            cur = self.client.get(index=self.ix("locks"), id=name)
            if cur["_source"]["owner"] == owner:
                self.client.delete(index=self.ix("locks"), id=name, if_seq_no=cur["_seq_no"], if_primary_term=cur["_primary_term"])
        except (NotFoundError, ConflictError):
            pass

    # ---------- small helpers ----------
    def search(self, index: str, **body) -> dict:
        return self.client.search(index=self.ix(index), **body).body

    def esql(self, query: str, params: list | None = None) -> list[dict]:
        res = self.client.esql.query(query=query, params=params or []).body
        cols = [c["name"] for c in res.get("columns", [])]
        return [dict(zip(cols, row, strict=False)) for row in res.get("values", [])]

    def mvt(self, index: str, field: str, z: int, x: int, y: int, **kwargs) -> bytes:
        resp = self.client.search_mvt(index=self.ix(index), field=field, zoom=z, x=x, y=y, **kwargs)
        return bytes(resp.body) if not isinstance(resp.body, bytes) else resp.body

    def count(self, index: str) -> int:
        try:
            return self.client.count(index=self.ix(index))["count"]
        except NotFoundError:
            return 0


# ---------------------------------------------------------------- setup (idempotent)


def ensure_inference(es: ES) -> dict:
    """Mistral endpoints inside Elasticsearch: embeddings for semantic_text, chat for ES|QL COMPLETION."""
    cfg = es.cfg
    out = {"embed": False, "chat": False, "completion": False}
    if not cfg.has_mistral:
        return out
    specs = [
        ("text_embedding", EMBED_ENDPOINT, cfg.model_embed, "embed"),
        ("chat_completion", CHAT_ENDPOINT, cfg.model_small, "chat"),
        ("completion", COMPLETION_ENDPOINT, cfg.model_small, "completion"),
    ]
    for task, name, model, key in specs:
        eid = es.ix(name)
        try:
            es.client.inference.get(task_type=task, inference_id=eid)
            out[key] = True
            continue
        except NotFoundError:
            pass
        try:
            es.client.inference.put(
                task_type=task,
                inference_id=eid,
                inference_config={"service": "mistral", "service_settings": {"api_key": cfg.mistral_api_key, "model": model}},
            )
            out[key] = True
        except ApiError as exc:
            log.warning("inference endpoint %s not created: %s", eid, exc)
    return out


def inference_works(es: ES, inference_id: str, task: str) -> bool:
    """True if an endpoint exists AND can actually run here (e.g. Elastic-hosted models need ML/EIS)."""
    try:
        if task == "rerank":
            es.client.inference.inference(inference_id=inference_id, task_type=task, input=["flood"], query="flood", timeout="20s")
        else:
            es.client.inference.inference(inference_id=inference_id, task_type=task, input=["flood"], timeout="20s")
        return True
    except Exception:  # noqa: BLE001
        return False


def semantic_inference_id(es: ES, mistral_embed: bool) -> str | None:
    """semantic_text model: Mistral embeddings when configured, else Elastic's multilingual E5 if it can run."""
    if mistral_embed:
        return es.ix(EMBED_ENDPOINT)
    if inference_works(es, E5_FALLBACK, "text_embedding"):
        return E5_FALLBACK
    return None


def ensure_synonyms(es: ES) -> bool:
    try:
        es.client.synonyms.put_synonym(id=es.ix(SYNONYMS_SET), synonyms_set=[{"id": f"r{i}", "synonyms": r} for i, r in enumerate(SYNONYMS)])
        return True
    except ApiError as exc:
        log.warning("synonyms set not created: %s", exc)
        return False


def _analysis(es: ES) -> dict:
    return {
        "filter": {"hg_syn": {"type": "synonym_graph", "synonyms_set": es.ix(SYNONYMS_SET), "updateable": True}},
        "analyzer": {
            "hg_index": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "asciifolding"]},
            "hg_search": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "asciifolding", "hg_syn"]},
        },
    }


def ensure_data_streams(es: ES) -> list[str]:
    created = []
    for name, retention in DATA_STREAMS.items():
        idx = es.ix(name)
        props = dict(MAPPINGS[name]["properties"])
        props.setdefault("@timestamp", DATE)
        es.client.indices.put_index_template(
            name=idx,
            index_patterns=[idx],
            data_stream={},
            priority=500,
            template={"lifecycle": {"data_retention": retention}, "mappings": {"properties": props}},
        )
        if not es.client.indices.exists(index=idx):
            es.client.indices.create_data_stream(name=idx)
            created.append(idx)
    return created


def ensure_indices(es: ES, semantic: bool | str | None = False) -> list[str]:
    """semantic: inference id for guides.content (or True = Mistral endpoint); falsy = plain text."""
    created = []
    ensure_synonyms(es)
    created += ensure_data_streams(es)
    for name, mapping in MAPPINGS.items():
        idx = es.ix(name)
        if name in DATA_STREAMS:
            continue
        if es.client.indices.exists(index=idx):
            try:  # additive mapping changes apply in place (new fields only)
                props = {k: v for k, v in mapping["properties"].items() if not (name == "guides" and k == "content")}
                es.client.indices.put_mapping(index=idx, properties=props)
            except ApiError as exc:
                log.info("mapping update skipped for %s: %s", idx, str(exc)[:200])
            continue
        m = {"properties": dict(mapping["properties"])}
        settings_ = dict(INDEX_SETTINGS.get(name, {}))
        if name == "guides":
            settings_["analysis"] = _analysis(es)
            inf = es.ix(EMBED_ENDPOINT) if semantic is True else semantic
            if inf:
                m["properties"]["content"] = {"type": "semantic_text", "inference_id": inf}
        es.client.indices.create(index=idx, mappings=m, settings=settings_ or None)
        created.append(idx)
    return created


EMERGENCY_TERMS = [
    "emergency", "trapped", "danger", "911", "drowning", "power line", "emergencia", "atrapado", "peligro",
    "緊急", "危險", "被困", "emerhensiya", "khẩn cấp",
]  # fmt: skip


def ensure_query_rules(es: ES, pinned_by_lang: dict[str, list[str]]) -> bool:
    """Query rules: emergency-style questions always surface 'call 911 / report to 311' guidance first,
    in the reader's language (one pinned rule per language, English as the catch-all)."""
    rules = []
    for lang, ids in sorted(pinned_by_lang.items()):
        rules.append({
            "rule_id": f"emergency-first-{lang}",
            "type": "pinned",
            "criteria": [
                {"type": "contains", "metadata": "query_string", "values": EMERGENCY_TERMS},
                {"type": "exact", "metadata": "lang", "values": [lang]},
            ],
            "actions": {"ids": ids[:10]},
        })  # fmt: skip
    if "en" in pinned_by_lang:
        rules.append({"rule_id": "emergency-first-any", "type": "pinned",
                      "criteria": [{"type": "contains", "metadata": "query_string", "values": EMERGENCY_TERMS}],
                      "actions": {"ids": pinned_by_lang["en"][:10]}})  # fmt: skip
    try:
        es.client.query_rules.put_ruleset(ruleset_id=es.ix(QUERY_RULESET), rules=rules)
        return True
    except ApiError as exc:
        log.warning("query rules not created: %s", exc)
        return False


def ensure_enrich_and_pipeline(es: ES) -> bool:
    """geo_match enrich (flood zones + neighborhoods) -> painless flags. Needs flood-zones loaded first."""
    if es.count("flood-zones") == 0:
        log.warning("flood-zones is empty; load layers before creating the enrich policy")
        return False
    policy = es.ix(ENRICH_POLICY)
    try:
        es.client.ingest.delete_pipeline(id=es.ix(PIPELINE))
    except NotFoundError:
        pass
    try:
        es.client.enrich.delete_policy(name=policy)
    except NotFoundError:
        pass
    es.client.enrich.put_policy(
        name=policy,
        geo_match={"indices": es.ix("flood-zones"), "match_field": "geometry", "enrich_fields": ["layer", "name"]},
    )
    es.client.enrich.execute_policy(name=policy, wait_for_completion=True)
    es.client.ingest.put_pipeline(
        id=es.ix(PIPELINE),
        description="HighGround: tag 311 reports with flood zone / SLR zone / neighborhood via geo_match enrich",
        processors=[
            {
                "enrich": {
                    "policy_name": policy,
                    "field": "location",
                    "target_field": "zones",
                    "shape_relation": "INTERSECTS",
                    "max_matches": 16,
                    "ignore_missing": True,
                    "ignore_failure": True,
                }
            },
            {
                "script": {
                    "lang": "painless",
                    "ignore_failure": True,
                    "source": """
                        ctx.zone_100yr = false; ctx.slr24 = false;
                        def z = ctx.zones;
                        if (z != null) {
                          def list = (z instanceof List) ? z : [z];
                          for (def m : list) {
                            if (m.layer == '100yr') { ctx.zone_100yr = true; }
                            if (m.layer == 'slr24') { ctx.slr24 = true; }
                            if (m.layer == 'neighborhood' && ctx.neighborhood == null) { ctx.neighborhood = m.name; }
                          }
                          ctx.remove('zones');
                        }
                    """,
                }
            },
            {"set": {"field": "ingested_at", "value": "{{{_ingest.timestamp}}}"}},
        ],
    )
    es._pipeline_ok = True
    return True


def ensure_transform(es: ES) -> bool:
    """Continuous pivot: hazard reports per H3 cell per hour (powers history charts and the replay timeline)."""
    tid = es.ix(TRANSFORM)
    try:
        es.client.transform.get_transform(transform_id=tid)
        return True
    except NotFoundError:
        pass
    es.client.transform.put_transform(
        transform_id=tid,
        source={"index": es.ix("hazard-reports")},
        dest={"index": es.ix("block-hourly")},
        pivot={
            "group_by": {
                "h3_9": {"terms": {"field": "h3_9"}},
                "hour": {"date_histogram": {"field": "requested_at", "fixed_interval": "1h"}},
            },
            "aggregations": {
                "total": {"value_count": {"field": "case_id"}},
                "flooding": {"filter": {"term": {"category": "flooding"}}},
                "tree": {"filter": {"term": {"category": "tree"}}},
            },
        },
        sync={"time": {"field": "ingested_at", "delay": "60s"}},
        frequency="5m",
        description="HighGround hazard reports per H3 res-9 cell per hour",
    )
    es.client.transform.start_transform(transform_id=tid)
    return True


def setup(es: ES) -> dict:
    inf = ensure_inference(es)
    sem = semantic_inference_id(es, inf["embed"])
    created = ensure_indices(es, semantic=sem)
    return {"inference": inf, "semantic_model": sem, "created": created,
            "rerank": inference_works(es, RERANK_ENDPOINT, "rerank")}  # fmt: skip


def iter_bulk(es: ES, index: str, docs: Iterable[dict], pipeline: str | None = None, merge_fn=None, batch: int = 2000) -> int:
    total, buf = 0, []
    for d in docs:
        buf.append(d)
        if len(buf) >= batch:
            total += es.upsert_docs(index, buf, pipeline, merge_fn)
            buf = []
    if buf:
        total += es.upsert_docs(index, buf, pipeline, merge_fn)
    return total
