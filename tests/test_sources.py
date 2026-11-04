"""Parsers and normalizers, against real responses captured on 2026-11-03 (tests/fixtures)."""

from collections import Counter
from datetime import UTC, datetime

from highground.models import Category
from highground.sources import apify, chp, datasf, noaa, nws, open311, places, weather
from highground.sources.hazards import classify_sewer_text, merge_case, parse_pacific


def test_open311_normalizes_real_sewer_feed(fixtures):
    docs = [d for d in (open311.normalize(r) for r in fixtures("open311_sewer.json")) if d]
    assert len(docs) == 60
    cats = Counter(d["category"] for d in docs)
    assert cats["flooding"] > 0 and cats["sewer_backup"] > 0
    d = docs[0]
    assert d["_id"].startswith("311:") and d["case_id"] in d["_id"]
    assert d["requested_at"].endswith("+00:00")
    assert len(d["h3_9"]) == 15 and d["seen_open311"] is True


def test_open311_tree_subcategory(fixtures):
    docs = [d for d in (open311.normalize(r) for r in fixtures("open311_tree.json")) if d]
    assert {d["category"] for d in docs} == {"tree"}
    assert {"maintenance", "fallen_or_damaged"} <= {d["subcategory"] for d in docs}


def test_keyword_classifier():
    assert classify_sewer_text("Street flooding at the corner, water over the curb") == Category.flooding
    assert classify_sewer_text("Sewage back up in my toilet") == Category.sewer
    assert classify_sewer_text("Water main leak") == Category.water_leak
    assert classify_sewer_text("Storm drain clogged with leaves") == Category.catch_basin
    assert classify_sewer_text("") == Category.other


def test_datasf_local_time_becomes_utc(fixtures):
    docs = [d for d in (datasf.normalize(r) for r in fixtures("datasf_311_20221231.json")) if d]
    assert docs and all(d["category_source"] == "datasf_subtype" for d in docs)
    # fixture window is 10:00-10:20 Pacific on Dec 31, 2022 = 18:00-18:20 UTC
    assert all(d["requested_at"].startswith("2022-12-31T18:") for d in docs)
    assert parse_pacific("2026-11-02T16:14:12.000").isoformat() == "2026-11-03T00:14:12+00:00"


def test_datasf_categories():
    assert datasf.categorize("Sewer Issues", "Flooding") == (Category.flooding, "flooding")
    assert datasf.categorize("Tree Maintenance", "overgrown_tree") is None
    assert datasf.categorize("Tree Maintenance", "Trees - Damaged_Tree")[0] == Category.tree


def test_merge_case_precedence():
    live = {"_id": "311:1", "category": "flooding", "category_source": "keywords", "status": "open", "updated_at": "2026-11-03T10:00:00+00:00", "media_url": "x"}
    nightly = {"_id": "311:1", "category": "sewer_backup", "category_source": "datasf_subtype", "status": "closed", "updated_at": "2026-11-03T12:00:00+00:00", "neighborhood": "Mission"}
    merged = merge_case(merge_case(None, live), nightly)
    assert merged["category"] == "sewer_backup" and merged["neighborhood"] == "Mission" and merged["media_url"] == "x"
    stale_live = {**live, "updated_at": "2026-11-03T11:00:00+00:00", "status": "open"}
    again = merge_case(merged, stale_live)
    assert again["category"] == "sewer_backup", "a live poll must not overwrite the DataSF subtype"
    assert again["status"] == "closed", "an older status must not overwrite a newer one"
    assert "zone_100yr" not in merge_case({"zone_100yr": True}, live)


def test_weather_parse_multi_point(fixtures, now):
    docs = weather.parse_hourly(fixtures("openmeteo_forecast.json"), now)
    assert {d["point"] for d in docs} == {"nw", "sw"}
    assert any(d["is_forecast"] for d in docs) and any(not d["is_forecast"] for d in docs)


def test_asos_fallback_parse(fixtures):
    docs = weather.parse_asos_csv(fixtures("asos_sfo_20221231.csv"))
    per_point = [d for d in docs if d["point"] == "c"]
    assert len(per_point) == 24
    assert 90 < sum(d["rain_mm"] for d in per_point) < 110  # ~3.85 in at SFO that UTC day


def test_noaa_parse(fixtures):
    preds = noaa.parse_predictions(fixtures("noaa_pred.json"))
    assert {p["tide_type"] for p in preds} == {"H", "L"}
    assert noaa.parse_observed(fixtures("noaa_latest.json"))[0]["kind"] == "tide_obs"
    assert len(noaa.parse_observed(fixtures("noaa_hourly_20221231.json"))) >= 24


def test_nws_alerts_and_iem_archive(fixtures):
    alerts = nws.parse_alerts(fixtures("nws_alerts.json"))
    assert "Coastal Flood Advisory" in {a["event"] for a in alerts}
    archived = nws.parse_iem(fixtures("iem_mtr_20221231.json"), "2022-12-31T08:00:00Z", "2023-01-01T08:00:00Z")
    events = {a["event"] for a in archived}
    assert {"Flood Watch", "Flood Advisory"} <= events
    assert all(set(a["ugc"]) <= set(nws.SF_UGC) for a in archived)


def test_chp_filters_to_sf(fixtures, now):
    rows = chp.parse(fixtures("chp_ggcc.xml"), now)
    assert rows and all(37.70 < r["location"]["lat"] < 37.82 for r in rows)


def test_places_normalization():
    assert places.normalize_query("17th and Folsom") == "17TH & FOLSOM"
    assert places.normalize_query("Folsom y 17th, San Francisco CA 94110") == "FOLSOM & 17TH"
    assert places.street_core("03RD ST") == "3RD" and places.street_core("THIRD") == "3RD"
    docs = places.intersection_docs([{"id": "1", "street_name_1": "17TH ST", "street_name_2": "FOLSOM ST", "latitude": "37.7637", "longitude": "-122.4152"}])
    assert {"17TH & FOLSOM", "FOLSOM & 17TH"} <= set(docs[0]["alt"])


def test_apify_guides_and_signals_keep_no_usernames():
    items = [{"url": "https://www.sfpuc.gov/es/x", "markdown": "# Título\n" + "texto " * 80, "metadata": {"title": "T"}}]
    g = apify.guide_docs(items, datetime(2026, 11, 3, tzinfo=UTC))
    assert g and g[0]["lang"] == "es"
    s = apify.signal_candidates([{"title": "Flooding at 17th and Folsom", "body": "knee deep", "url": "https://reddit.com/x", "createdAt": "2026-11-03T10:00:00Z", "username": "someone", "userId": "u1"}])
    assert s == [{"source": "reddit", "url": "https://reddit.com/x", "published_at": "2026-11-03T10:00:00Z", "text": "Flooding at 17th and Folsom knee deep"}]
    assert "someone" not in str(s)
