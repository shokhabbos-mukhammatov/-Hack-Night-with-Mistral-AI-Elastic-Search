"""Replay must never look ahead; backtest metrics must be computed exactly as documented."""

from datetime import UTC, datetime, timedelta

import pytest

from highground.geo import cell_center
from highground.replay import DATA_DIR, ReplayDataset, ReplayInputs, backtest, relevel, run

CELL = "89283082c6fffff"
LAT, LON = cell_center(CELL)


def tiny(date="2022-12-31") -> ReplayDataset:
    hours = [f"2022-12-31T{h:02d}:00:00+00:00" for h in range(8, 24)] + [f"2023-01-01T{h:02d}:00:00+00:00" for h in range(0, 9)]
    obs = {"c": [(t, 20.0 if "T1" in t[10:13] else 0.0, 30.0) for t in hours]}  # heavy rain 10:00-19:59 UTC
    return ReplayDataset(
        date=date,
        weather_obs=obs,
        weather_fc=obs,
        tide=[(t, 4.0) for t in hours],
        alerts=[{"event": "Flood Advisory", "valid_from": "2022-12-31T13:00:00Z", "valid_to": "2022-12-31T23:00:00Z", "issued_at": "2022-12-31T12:30:00Z"}],
        reports=[{"t": "2022-12-31T20:00:00+00:00", "lat": LAT, "lon": LON, "cell": CELL, "category": "flooding", "case_id": "1"}],
        profiles={CELL: {"zone": {"in_100yr": True, "slr24": False, "shoreline": False, "depth_ft": None}, "reports_5y": 40, "storm_reports_5y": 30, "storm_days_5y": 12}},
    )


def test_no_look_ahead():
    prov = ReplayInputs(tiny())
    t = datetime(2022, 12, 31, 12, 0, tzinfo=UTC)
    i = prov.inputs_for(CELL, t)
    assert not [e for e in i.events if e.at > t], "a report at 20:00 must be invisible at 12:00"
    assert i.alerts == [], "the advisory was issued at 12:30, so it is unknown at 12:00"
    assert i.weather.rain_3h_mm == pytest.approx(60.0), "observed = the 10:00, 11:00, 12:00 readings only"
    assert prov.weather("c", datetime(2022, 12, 31, 9, 30, tzinfo=UTC)).rain_3h_mm == 0.0, "rain starting at 10:00 is unknown at 09:30"
    later = prov.inputs_for(CELL, datetime(2022, 12, 31, 20, 30, tzinfo=UTC))
    assert len(later.events) == 1 and later.alerts


def test_backtest_lead_time_and_relevel():
    ds = tiny()
    res = run(ds)
    bt = backtest(ds, res)
    assert bt["flooded_cells"] == 1
    assert bt["warned_before_first_report"] == 1 and bt["median_lead_min"] > 0
    impossible = backtest(ds, res, thresholds=(98, 99, 100))
    assert impossible["warned_before_first_report"] == 0 and impossible["cells_ever_high"] == 0
    lv = relevel(res, (0.1, 0.2, 0.3))
    assert set(lv) == set(res["scores"])


@pytest.mark.skipif(not (DATA_DIR / "2022-12-31.json").exists(), reason="replay dataset not prepared")
def test_real_dataset_loads_and_is_well_formed():
    ds = ReplayDataset.load("2022-12-31")
    assert len(ds.reports) > 300 and len(ds.profiles) > 1000
    assert all(r["t"] >= "2022-12-30" for r in ds.reports)
    assert "2022-12-31" not in ds.storm_days, "profiles must be computed strictly before the replay day"
    d0 = datetime(2022, 12, 31, 8, tzinfo=UTC)
    assert any(datetime.fromisoformat(r["t"]) < d0 + timedelta(hours=24) for r in ds.reports)
