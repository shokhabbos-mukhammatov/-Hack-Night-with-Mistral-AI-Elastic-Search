"""Integration tests against a real Elasticsearch (throwaway index prefix). Skipped when none is reachable.

Covers: mappings, geo_match enrich pipeline, merge-on-ingest, percolator, ES|QL, vector tiles,
lease locks, places geocoding, the outbox's idempotency, and the live risk loop.
"""

from datetime import UTC, datetime, timedelta

import pytest

from highground import alerts, riskloop, tools
from highground import es as es_mod
from highground.geo import cell_center, cell_of
from highground.geocode import geocode
from highground.sources import places
from highground.sources.hazards import merge_case
from highground.sources.open311 import normalize

pytestmark = pytest.mark.integration

FOLSOM = (37.76369683, -122.41515522)
ZONE = {"type": "Polygon", "coordinates": [[[-122.4175, 37.7620], [-122.4130, 37.7620], [-122.4130, 37.7655], [-122.4175, 37.7655], [-122.4175, 37.7620]]]}
NHOOD = {"type": "Polygon", "coordinates": [[[-122.43, 37.75], [-122.40, 37.75], [-122.40, 37.775], [-122.43, 37.775], [-122.43, 37.75]]]}


@pytest.fixture(scope="module")
def es(test_es):
    es_mod.ensure_indices(test_es)
    test_es.upsert_docs(
        "flood-zones",
        [
            {"_id": "z1", "layer": "100yr", "name": "test zone", "geometry": ZONE},
            {"_id": "n1", "layer": "neighborhood", "name": "Mission", "geometry": NHOOD},
        ],
        refresh="wait_for",
    )
    assert es_mod.ensure_enrich_and_pipeline(test_es)
    return test_es


def report(case_id, lat, lon, minutes_ago, desc="Flooding, water over the curb"):
    t = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return normalize({"service_request_id": case_id, "service_code": "PUC:Water:Sewer", "service_name": "Flooding", "description": desc,
                      "requested_datetime": t, "updated_datetime": t, "lat": lat, "long": lon, "status": "open", "address": "17th & Folsom"})  # fmt: skip


def test_enrich_pipeline_tags_zone_and_neighborhood(es):
    docs = [report("9001", *FOLSOM, 20), report("9002", 37.7700, -122.4250, 30)]
    assert es.upsert_docs("hazard-reports", docs, "reports-enrich", merge_case, refresh="wait_for") == 2
    a = es.client.get(index=es.ix("hazard-reports"), id="311:9001")["_source"]
    b = es.client.get(index=es.ix("hazard-reports"), id="311:9002")["_source"]
    assert a["zone_100yr"] is True and a["neighborhood"] == "Mission" and "zones" not in a
    assert b["zone_100yr"] is False


def test_merge_on_ingest_keeps_authoritative_fields(es):
    nightly = {**report("9001", *FOLSOM, 20), "category": "sewer_backup", "category_source": "datasf_subtype", "neighborhood": "Mission"}
    es.upsert_docs("hazard-reports", [nightly], "reports-enrich", merge_case, refresh="wait_for")
    es.upsert_docs("hazard-reports", [report("9001", *FOLSOM, 20)], "reports-enrich", merge_case, refresh="wait_for")
    doc = es.client.get(index=es.ix("hazard-reports"), id="311:9001")["_source"]
    assert doc["category"] == "sewer_backup" and doc["zone_100yr"] is True


def test_queries_zone_nearby_esql(es):
    es.upsert_docs("hazard-reports", [report("9003", *FOLSOM, 10)], "reports-enrich", merge_case, refresh="wait_for")
    assert tools.zone_at(es, *FOLSOM)["in_100yr"] is True
    ev = tools.nearby_events(es, *FOLSOM, datetime.now(UTC))
    assert any(e.kind.value == "flood_report" and e.distance_m < 50 for e in ev)
    hist = tools.block_history(es, *FOLSOM)
    assert hist["top_days"] and hist["top_days"][0]["reports"] >= 1


def test_vector_tiles(es):
    lat, lon = FOLSOM
    import math

    z = 14
    x = int((lon + 180) / 360 * 2**z)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z)
    data = es.mvt("hazard-reports", "location", z, x, y, fields=["category"], grid_precision=0, exact_bounds=True)
    assert isinstance(data, bytes) and len(data) > 20


def test_percolator_event_alerts_and_outbox_idempotency(es):
    sub = alerts.subscribe(es, "telegram", {"chat_id": 123}, *FOLSOM, label="17th & Folsom", confirmed=True)
    events = [{"id": "ev1", "location": {"lat": FOLSOM[0] + 0.0005, "lon": FOLSOM[1]}, "kind": "flood_report", "label": "Folsom St", "cell": cell_of(*FOLSOM)}]
    assert alerts.event_alerts(es, events) == 1
    es.client.update(index=es.ix("subscriptions"), id=sub["subscription_id"], doc={"last_alert": None}, refresh="wait_for")
    assert alerts.event_alerts(es, events) == 0, "same event id -> same outbox _id -> no duplicate"
    far = [{"id": "ev2", "location": {"lat": 37.80, "lon": -122.45}, "kind": "flood_report", "label": "far", "cell": "x"}]
    assert alerts.event_alerts(es, far) == 0


def test_dispatch_sends_and_marks(es):
    from highground.messaging.base import SendResult

    class Fake:
        name = "telegram"
        sent = []

        def send(self, handle, text, url=None):
            self.sent.append((handle["chat_id"], text))
            return SendResult(ok=True)

    es.client.indices.refresh(index=es.ix("alerts-outbox"))
    fake = Fake()
    stats = alerts.dispatch(es, {"telegram": fake})
    assert stats["sent"] >= 1 and fake.sent[0][0] == 123
    es.client.indices.refresh(index=es.ix("alerts-outbox"))
    assert alerts.dispatch(es, {"telegram": fake})["sent"] == 0


def test_stop_deletes_subscription(es):
    alerts.subscribe(es, "telegram", {"chat_id": 555}, *FOLSOM, label="x", confirmed=True)
    assert alerts.set_status_for_handle(es, "telegram", "555", "stopped") == 1


def test_lease_lock(es):
    now = datetime.now(UTC)
    assert es.acquire_lock("t", "a", 60, now)
    assert es.acquire_lock("t", "a", 60, now), "the owner can renew"
    assert not es.acquire_lock("t", "b", 60, now), "others wait"
    assert es.acquire_lock("t", "b", 60, now + timedelta(seconds=61)), "an expired lease can be taken over"


def test_places_geocoder(es):
    docs = places.intersection_docs([{"id": "1", "street_name_1": "17TH ST", "street_name_2": "FOLSOM ST", "latitude": str(FOLSOM[0]), "longitude": str(FOLSOM[1])}])
    docs += places.address_docs([{"eas_baseid": "1", "address": "2450 HARRISON ST", "street_type": "ST", "latitude": "37.75815", "longitude": "-122.41259", "nhood": "Mission"}])
    es.upsert_docs("places", docs, refresh="wait_for")
    assert geocode(es, "17th and folsom st")["label"] == "17TH ST & FOLSOM ST"
    assert geocode(es, "folsom y 17th")["kind"] == "intersection"
    assert geocode(es, "2450 Harison")["label"] == "2450 HARRISON ST"


def test_live_risk_loop_writes_cells(es):
    c = cell_of(*FOLSOM)
    lat, lon = cell_center(c)
    es.upsert_docs("cell-profiles", [{"_id": c, "cell": c, "in_100yr": True, "slr24": False, "shoreline": False, "reports_5y": 40,
                                      "storm_reports_5y": 30, "storm_days_5y": 12, "centroid": {"lat": lat, "lon": lon}}], refresh="wait_for")  # fmt: skip
    out = riskloop.run_once(es)
    assert out["cells"] == 1
    es.client.indices.refresh(index=es.ix("risk-cells"))
    doc = es.client.get(index=es.ix("risk-cells"), id=c)["_source"]
    assert doc["level"] in ("low", "elevated", "high", "severe") and doc["polygon"]["type"] == "Polygon"
    assert riskloop.run_once(es)["changed"] == 0, "a stable level is not re-reported"
