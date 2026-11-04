from datetime import UTC, datetime, timedelta

from highground.models import (
    CellProfile,
    EventKind,
    Inputs,
    Level,
    NearbyEvent,
    NwsAlert,
    Tide,
    Weather,
    ZoneInfo,
)
from highground.risk import assess, inputs_hash, level_for, smooth_level

NOW = datetime(2022, 12, 31, 18, 0, tzinfo=UTC)
CELL = "89283082c6fffff"


def inp(zone=None, rain=(0, 0, 0), gust=0, alerts=(), events=(), tide=None, hist=(0, 12)) -> Inputs:
    return Inputs(
        cell=CELL,
        as_of=NOW,
        profile=CellProfile(cell=CELL, zone=zone or ZoneInfo(), storm_reports_5y=hist[0], storm_days_5y=hist[1]),
        weather=Weather(point="c", rain_1h_mm=rain[0], rain_3h_mm=rain[1], rain_next3h_mm=rain[2], gust_kmh=gust, as_of=NOW),
        alerts=list(alerts),
        tide=tide,
        events=list(events),
    )


def ev(kind, minutes=10, dist=100, **kw):
    return NearbyEvent(kind=kind, at=NOW - timedelta(minutes=minutes), distance_m=dist, source="test", **kw)


def test_dry_day_is_low_even_in_flood_zone():
    st = assess(inp(zone=ZoneInfo(in_100yr=True), hist=(30, 12)))
    assert st.level == Level.low
    assert {"zone_100yr", "history"} <= {r.code for r in st.reasons}


def test_design_storm_in_flood_zone():
    """Calibrated behaviour (data/replay/calibration.json): rain alone at a hotspot = Elevated;
    with the NWS flood product such storms carry, it crosses High."""
    rain_only = assess(inp(zone=ZoneInfo(in_100yr=True), rain=(15, 35, 20), hist=(30, 12)))
    assert rain_only.level == Level.elevated and "design_storm" in {r.code for r in rain_only.reasons}
    advisory = NwsAlert(id="a", event="Flood Advisory", starts=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=4))
    with_nws = assess(inp(zone=ZoneInfo(in_100yr=True), rain=(15, 35, 20), hist=(30, 12), alerts=[advisory]))
    assert with_nws.level.rank >= Level.high.rank


def test_same_rain_on_a_hilltop_is_lower():
    flat = assess(inp(zone=ZoneInfo(in_100yr=True), rain=(15, 35, 20), hist=(30, 12)))
    hill = assess(inp(rain=(15, 35, 20)))
    assert hill.score < flat.score and hill.level.rank < flat.level.rank


def test_nws_flood_warning_raises_hazard():
    base = assess(inp(rain=(5, 12, 5)))
    warned = assess(inp(rain=(5, 12, 5), alerts=[NwsAlert(id="1", event="Flood Warning", starts=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=3))]))
    assert warned.score > base.score and any(r.code == "nws:Flood Warning" for r in warned.reasons)


def test_expired_or_future_alerts_are_ignored():
    past = NwsAlert(id="p", event="Flood Warning", starts=NOW - timedelta(hours=5), ends=NOW - timedelta(hours=1))
    future = NwsAlert(id="f", event="Flood Warning", starts=NOW + timedelta(hours=1), ends=NOW + timedelta(hours=5))
    assert assess(inp(alerts=[past, future])).score == assess(inp()).score


def test_unverified_signals_alone_cannot_make_high():
    many_posts = [ev(EventKind.street_signal, minutes=5, dist=50, confidence=1.0) for _ in range(20)]
    st = assess(inp(zone=ZoneInfo(in_100yr=True), rain=(2, 5, 2), events=many_posts))
    assert st.level.rank <= Level.elevated.rank


def test_flood_reports_nearby_confirm():
    reports = [ev(EventKind.flood_report, minutes=m) for m in (5, 15, 30)]
    st = assess(inp(rain=(5, 15, 5), events=reports))
    assert any(r.code == "reports_nearby" for r in st.reasons) and st.components.confirmation > 1


def test_events_from_the_future_never_count():
    future = NearbyEvent(kind=EventKind.flood_report, at=NOW + timedelta(minutes=30), distance_m=50, source="t")
    assert assess(inp(events=[future])).score == assess(inp()).score


def test_coastal_only_for_shoreline_cells():
    tide = Tide(next_high_ft=7.4, next_high_at=NOW + timedelta(hours=2))
    alert = NwsAlert(id="c", event="Coastal Flood Advisory", starts=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=6))
    inland = assess(inp(tide=tide, alerts=[alert]))
    shore = assess(inp(zone=ZoneInfo(slr24=True, shoreline=True), tide=tide, alerts=[alert]))
    assert shore.score > inland.score and any(r.code == "high_tide" for r in shore.reasons)


def test_wind_on_wet_soil_adds_tree_risk():
    st = assess(inp(rain=(2, 12, 0), gust=95))
    assert st.components.trees > 0 and any(r.code == "wind" for r in st.reasons)


def test_levels_and_hysteresis():
    assert level_for(0.5) == Level.low and level_for(2.5) == Level.high and level_for(3.0) == Level.severe
    assert smooth_level(Level.low, Level.high, 0) == (Level.high, 0)
    assert smooth_level(Level.high, Level.low, 0) == (Level.high, 1)
    assert smooth_level(Level.high, Level.low, 1) == (Level.low, 0)


def test_inputs_hash_stable_and_sensitive():
    a, b = inp(rain=(1, 2, 3)), inp(rain=(1, 2, 3))
    assert inputs_hash(a) == inputs_hash(b)
    assert inputs_hash(a) != inputs_hash(inp(rain=(20, 40, 3)))
