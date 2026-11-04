"""Storm Replay + backtest: re-run the live risk engine over a real archived storm day, with no look-ahead.

    python -m highground.replay prepare 2022-12-31     # fetch archives -> data/replay/2022-12-31.json
    python -m highground.replay backtest 2022-12-31    # lead time / false alarms, printed as JSON

The dataset is materialized once (real archives only):
  rain + gusts   Open-Meteo archive (reanalysis) per SF weather point; next-3h forecast from the
                 Open-Meteo historical-forecast API (what models said at the time)
  tide           NOAA CO-OPS hourly heights at the SF gauge
  NWS products   IEM VTEC archive for SF zones (as issued)
  311 reports    DataSF 311 cases for the day, at their true timestamps
  profiles       cell susceptibility computed from ES history strictly before the replay day
"""

import argparse
import json
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from highground import http as http_mod
from highground.geo import cell_center, cell_of, haversine_m, nearest_weather_point, ring
from highground.models import CellProfile, EventKind, Inputs, Level, NearbyEvent, NwsAlert, Tide, Weather
from highground.models import ZoneInfo as Zone
from highground.risk import DEFAULT, RiskParams, assess
from highground.sources import datasf, noaa, nws, weather
from highground.sources.hazards import parse_utc

PACIFIC = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "replay"
STEP = timedelta(minutes=15)
FLOOD_CATS = {"flooding": EventKind.flood_report, "catch_basin": EventKind.flood_report, "sewer_backup": EventKind.sewer_report}


@dataclass
class ReplayDataset:
    date: str
    weather_obs: dict[str, list[tuple[str, float, float]]]  # point -> [(iso_hour, rain_mm, gust_kmh)]
    weather_fc: dict[str, list[tuple[str, float, float]]]
    tide: list[tuple[str, float]]
    alerts: list[dict]
    reports: list[dict]  # {t, lat, lon, cell, category, subcategory, address, case_id}
    profiles: dict[str, dict]
    storm_days: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    extra_events: list[dict] = field(default_factory=list)  # {t, lat, lon, cell, kind, label, confidence, url}

    def save(self, path: Path | None = None) -> Path:
        path = path or DATA_DIR / f"{self.date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, separators=(",", ":")))
        return path

    @classmethod
    def load(cls, date: str, path: Path | None = None) -> "ReplayDataset":
        return cls(**json.loads((path or DATA_DIR / f"{date}.json").read_text()))


def day_bounds(date: str) -> tuple[datetime, datetime]:
    """Local (Pacific) calendar day as UTC instants."""
    start = datetime.fromisoformat(date).replace(tzinfo=PACIFIC)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


# ---------------------------------------------------------------- prepare (network + ES for profiles)


def prepare(date: str, es=None) -> ReplayDataset:
    d0, d1 = day_bounds(date)
    a, b = (d0 - timedelta(days=1)).date().isoformat(), (d1 + timedelta(days=1)).date().isoformat()
    with http_mod.client(timeout=120) as http:
        obs_docs, fc_docs = weather.archive(http, a, b)
        tide_docs = noaa.archive_hourly(http, a.replace("-", ""), b.replace("-", ""))
        alert_docs = nws.archive(http, d0.year, (d0 - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%MZ"), d1.strftime("%Y-%m-%dT%H:%MZ"))
        if d1.year != d0.year:
            alert_docs += nws.archive(http, d1.year, d0.strftime("%Y-%m-%dT%H:%MZ"), d1.strftime("%Y-%m-%dT%H:%MZ"))
        win0, win1 = d0 - timedelta(hours=8), d1
        reports = []
        for doc in datasf.backfill(http, win0, win1):
            if doc["category"] not in (*FLOOD_CATS, "tree"):
                continue
            if doc["category"] == "tree" and doc.get("subcategory") != "fallen_or_damaged":
                continue
            reports.append(
                {
                    "t": doc["requested_at"],
                    "lat": doc["location"]["lat"],
                    "lon": doc["location"]["lon"],
                    "cell": doc["h3_9"],
                    "category": doc["category"],
                    "subcategory": doc.get("subcategory"),
                    "address": doc.get("address"),
                    "case_id": doc["case_id"],
                }
            )
    profiles, storm_days = {}, []
    if es is not None:
        from highground.profiles import compute_profiles

        prof, meta = compute_profiles(es, d0)
        storm_days = meta["storm_days"]
        profiles = {c: p.model_dump(exclude={"top_dates"}) for c, p in prof.items()}

    def series(docs):
        out = defaultdict(list)
        for d in sorted(docs, key=lambda x: x["@timestamp"]):
            out[d["point"]].append((d["@timestamp"], d["rain_mm"] or 0.0, d["gust_kmh"] or 0.0))
        return dict(out)

    seen_alerts = {}
    for al in alert_docs:
        seen_alerts[al["_id"]] = {k: al.get(k) for k in ("event", "valid_from", "valid_to", "issued_at", "url", "ugc", "headline")}
    return ReplayDataset(
        date=date,
        weather_obs=series(obs_docs),
        weather_fc=series(fc_docs),
        tide=sorted((d["@timestamp"], d["value"]) for d in tide_docs),
        alerts=list(seen_alerts.values()),
        reports=sorted(reports, key=lambda r: r["t"]),
        profiles=profiles,
        storm_days=storm_days,
        sources={
            "weather_obs": sorted({d["source"] for d in obs_docs}),
            "weather_fc": sorted({d["source"] for d in fc_docs}) or ["none (archived forecast unavailable)"],
            "tide": noaa.API + f"?station={noaa.STATION}&product=hourly_height",
            "alerts": nws.IEM + "?wfo=MTR",
            "reports": datasf.DATASET,
            "prepared_at": datetime.now(UTC).isoformat(),
        },
    )


# ---------------------------------------------------------------- in-memory provider (same seam as LiveInputs)


class ReplayInputs:
    """InputsProvider over a ReplayDataset. Every lookup is cut off at as_of: nothing from the future leaks in."""

    def __init__(self, ds: ReplayDataset):
        self.ds = ds
        self._obs = {p: [(parse_utc(t), r, g) for t, r, g in rows] for p, rows in ds.weather_obs.items()}
        self._fc = {p: [(parse_utc(t), r, g) for t, r, g in rows] for p, rows in ds.weather_fc.items()}
        self._tide = [(parse_utc(t), v) for t, v in ds.tide]
        self._alerts = [
            NwsAlert(
                id=f"{a['event']}:{a['valid_from']}",
                event=a["event"],
                headline=a.get("headline"),
                starts=parse_utc(a["valid_from"]),
                ends=parse_utc(a["valid_to"]),
                url=a.get("url"),
            )
            for a in ds.alerts
        ]
        self._issued = {f"{a['event']}:{a['valid_from']}": parse_utc(a.get("issued_at") or a["valid_from"]) for a in ds.alerts}
        self._reports = [{**r, "_t": parse_utc(r["t"])} for r in ds.reports]
        self._by_cell = defaultdict(list)
        for r in self._reports:
            self._by_cell[r["cell"]].append(r)
        for e in ds.extra_events:
            self._by_cell[e["cell"]].append({**e, "_t": parse_utc(e["t"]), "category": "_extra"})

    def cells(self) -> list[str]:
        return sorted(self.ds.profiles)

    def profile(self, cell: str) -> CellProfile:
        p = self.ds.profiles.get(cell)
        if not p:
            return CellProfile(cell=cell)
        return CellProfile(
            cell=cell,
            zone=Zone(**p["zone"]),
            reports_5y=p["reports_5y"],
            storm_reports_5y=p["storm_reports_5y"],
            storm_days_5y=p["storm_days_5y"],
        )

    def weather(self, point: str, as_of: datetime) -> Weather:
        obs = self._obs.get(point, [])
        fc = self._fc.get(point, [])
        past = [(t, r, g) for t, r, g in obs if as_of - timedelta(hours=3) < t <= as_of]
        fut = [(t, r, g) for t, r, g in fc if as_of < t <= as_of + timedelta(hours=3)]
        return Weather(
            point=point,
            rain_1h_mm=round(sum(r for t, r, _ in past if t > as_of - timedelta(hours=1)), 2),
            rain_3h_mm=round(sum(r for _, r, _ in past), 2),
            rain_next3h_mm=round(sum(r for _, r, _ in fut), 2),
            gust_kmh=max([g for t, _, g in past if t > as_of - timedelta(hours=1)] or [0.0]),
            gust_next3h_kmh=max([g for _, _, g in fut] or [0.0]),
            as_of=max((t for t, _, _ in past), default=as_of),
            source="open-meteo-archive",
        )

    def tide(self, as_of: datetime) -> Tide:
        times = [t for t, _ in self._tide]
        i = bisect_right(times, as_of)
        now = self._tide[i - 1] if i else None
        # tide tables are published far ahead, so the next 12 h of (observed) heights stand in for predictions
        fut = [(t, v) for t, v in self._tide[i:] if t <= as_of + timedelta(hours=12)]
        hi = max(fut, key=lambda x: x[1]) if fut else None
        return Tide(now_ft=now[1] if now else None, next_high_ft=hi[1] if hi else None, next_high_at=hi[0] if hi else None, as_of=now[0] if now else None)

    def alerts(self, as_of: datetime) -> list[NwsAlert]:
        return [a for a in self._alerts if self._issued[a.id] <= as_of]

    def events(self, lat: float, lon: float, as_of: datetime, hours: float = 6, radius_m: float = 300) -> list[NearbyEvent]:
        since = as_of - timedelta(hours=hours)
        out = []
        for c in ring(cell_of(lat, lon), 3):
            for r in self._by_cell.get(c, ()):
                if not (since <= r["_t"] <= as_of):
                    continue
                dist = haversine_m(lat, lon, r["lat"], r["lon"])
                limit = 800 if r["category"] == "_extra" and r.get("kind") == "road_incident" else 500 if r["category"] == "_extra" else radius_m
                if dist > limit:
                    continue
                if r["category"] == "_extra":
                    out.append(
                        NearbyEvent(
                            kind=EventKind(r["kind"]),
                            at=r["_t"],
                            distance_m=round(dist),
                            confidence=r.get("confidence", 1.0),
                            source=r.get("source", r["kind"]),
                            source_url=r.get("url"),
                            label=r.get("label"),
                            lat=r["lat"],
                            lon=r["lon"],
                        )
                    )
                    continue
                kind = EventKind.tree_report if r["category"] == "tree" else FLOOD_CATS.get(r["category"])
                if not kind:
                    continue
                out.append(
                    NearbyEvent(
                        kind=kind,
                        at=r["_t"],
                        distance_m=round(dist),
                        verified_photo=bool(r.get("verified_photo")),
                        source=r.get("source", "SF311 (DataSF)"),
                        label=f"{r['category']}: {r.get('address') or ''}".strip(),
                        lat=r["lat"],
                        lon=r["lon"],
                    )
                )
        return out

    def inputs_for(self, cell: str, as_of: datetime, point: tuple[float, float] | None = None) -> Inputs:
        lat, lon = point or cell_center(cell)
        return Inputs(
            cell=cell,
            as_of=as_of,
            profile=self.profile(cell),
            weather=self.weather(nearest_weather_point(lat, lon), as_of),
            alerts=self.alerts(as_of),
            tide=self.tide(as_of),
            events=self.events(lat, lon, as_of),
        )

    def reports_between(self, a: datetime, b: datetime) -> list[dict]:
        return [r for r in self._reports if a <= r["_t"] < b]


# ---------------------------------------------------------------- timeline + backtest (pure over the dataset)


def steps(date: str, step: timedelta = STEP) -> list[datetime]:
    d0, d1 = day_bounds(date)
    out, t = [], d0
    while t < d1:
        out.append(t)
        t += step
    return out


def run(ds: ReplayDataset, p: RiskParams = DEFAULT, step: timedelta = STEP) -> dict:
    """Risk for every cell at every step. Returns compact series for the UI and the backtest."""
    prov = ReplayInputs(ds)
    ts = steps(ds.date, step)
    cells = prov.cells()
    levels: dict[str, list[int]] = {c: [] for c in cells}
    scores: dict[str, list[float]] = {c: [] for c in cells}
    city = []
    for t in ts:
        counts = [0, 0, 0, 0]
        for c in cells:
            st = assess(prov.inputs_for(c, t), p)
            levels[c].append(st.level.rank)
            scores[c].append(st.score)
            counts[st.level.rank] += 1
        wx = {pt: prov.weather(pt, t) for pt in ds.weather_obs}
        city.append(
            {
                "t": t.isoformat(),
                "local": t.astimezone(PACIFIC).strftime("%H:%M"),
                "levels": counts,
                "rain_1h_max_mm": max((w.rain_1h_mm or 0) for w in wx.values()) if wx else 0,
                "rain_3h_max_mm": max((w.rain_3h_mm or 0) for w in wx.values()) if wx else 0,
                "tide_ft": prov.tide(t).now_ft,
                "alerts": sorted({a.event for a in prov.alerts(t) if a.starts and a.starts <= t and (a.ends is None or t < a.ends)}),
                "reports_15m": len([r for r in prov.reports_between(t, t + step) if r["category"] in FLOOD_CATS]),
            }
        )
    return {"date": ds.date, "steps": [x["t"] for x in city], "city": city, "levels": levels, "scores": scores}


def relevel(result: dict, thresholds: tuple[float, float, float]) -> dict[str, list[int]]:
    """Pure: re-apply thresholds to stored scores (calibration without re-running the engine)."""
    e, h, sv = thresholds
    return {c: [3 if x >= sv else 2 if x >= h else 1 if x >= e else 0 for x in s] for c, s in result["scores"].items()}


def backtest(ds: ReplayDataset, result: dict | None = None, p: RiskParams = DEFAULT, thresholds=None) -> dict:
    """Lead time = first report in a cell minus the first step the cell was >= High (positive = warned first).

    precision = share of cells that ever reached High that had a flooding report within ring(1) that day
    recall    = share of flooded cells that were at High before (or at) their first report
    """
    result = result or run(ds, p)
    thresholds = tuple(thresholds or p.thresholds)
    levels = relevel(result, thresholds) if "scores" in result else result["levels"]
    result = {**result, "levels": levels}
    ts = [parse_utc(t) for t in result["steps"]]
    d0, d1 = day_bounds(ds.date)
    first_report: dict[str, datetime] = {}
    for r in ds.reports:
        t = parse_utc(r["t"])
        if r["category"] == "flooding" and d0 <= t < d1 and r["cell"] not in first_report:
            first_report[r["cell"]] = t
    flooded_near = set()
    for c in first_report:
        flooded_near.update(ring(c, 1))
    high = Level.high.rank
    leads, warned, missed = [], 0, 0
    per_cell = []
    for c, t_rep in first_report.items():
        series = result["levels"].get(c)
        if not series:
            continue
        t_high = next((ts[i] for i, lv in enumerate(series) if lv >= high), None)
        lead_min = (t_rep - t_high).total_seconds() / 60 if t_high else None
        if t_high and t_high <= t_rep:
            warned += 1
            leads.append(lead_min)
        else:
            missed += 1
        per_cell.append({"cell": c, "first_report": t_rep.isoformat(), "first_high": t_high.isoformat() if t_high else None, "lead_min": lead_min})
    high_cells = [c for c, s in result["levels"].items() if any(lv >= high for lv in s)]
    false_alarms = [c for c in high_cells if c not in flooded_near]
    return {
        "date": ds.date,
        "flooded_cells": len(first_report),
        "warned_before_first_report": warned,
        "missed_or_late": missed,
        "median_lead_min": statistics.median(leads) if leads else None,
        "lead_min_p25_p75": [statistics.quantiles(leads, n=4)[0], statistics.quantiles(leads, n=4)[2]] if len(leads) >= 4 else None,
        "cells_ever_high": len(high_cells),
        "false_alarm_cells": len(false_alarms),
        "false_alarm_share": round(len(false_alarms) / len(high_cells), 3) if high_cells else None,
        "first_report_citywide": min(first_report.values()).isoformat() if first_report else None,
        "first_high_citywide": next((ts[i].isoformat() for i in range(len(ts)) if any(s[i] >= high for s in result["levels"].values())), None),
        "precision": round(1 - len(false_alarms) / len(high_cells), 3) if high_cells else None,
        "recall": round(warned / len(first_report), 3) if first_report else None,
        "per_cell": sorted(per_cell, key=lambda x: x["first_report"]),
        "params": {"thresholds": list(thresholds)},
    }


def calibrate(results: list[tuple[ReplayDataset, dict]], grid_high=None, grid_elev_ratio=(0.5,), beta: float = 0.5) -> dict:
    """Grid-search the High threshold on training days, maximizing F-beta (beta<1 favours precision:
    false alarms erode trust). Severe = High x 1.5; Elevated = High x ratio."""
    grid_high = grid_high or [round(1.6 + 0.2 * i, 2) for i in range(15)]
    best, table = None, []
    for hi in grid_high:
        for r in grid_elev_ratio:
            th = (round(hi * r, 2), hi, round(hi * 1.5, 2))
            ps, rs, leads = [], [], []
            for ds, res in results:
                bt = backtest(ds, res, thresholds=th)
                if bt["precision"] is not None:
                    ps.append(bt["precision"])
                if bt["recall"] is not None:
                    rs.append(bt["recall"])
                if bt["median_lead_min"] is not None:
                    leads.append(bt["median_lead_min"])
            prec = statistics.mean(ps) if ps else 0.0
            rec = statistics.mean(rs) if rs else 0.0
            f = (1 + beta**2) * prec * rec / (beta**2 * prec + rec) if prec + rec else 0.0
            row = {"thresholds": th, "precision": round(prec, 3), "recall": round(rec, 3), "f_beta": round(f, 3),
                   "median_lead_min": statistics.median(leads) if leads else None}  # fmt: skip
            table.append(row)
            if best is None or f > best["f_beta"]:
                best = row
    return {"best": best, "table": table, "train_days": [ds.date for ds, _ in results], "beta": beta}


def publish(ds: ReplayDataset, p: RiskParams = DEFAULT) -> Path:
    """Precompute the UI payload: city timeline, per-cell level strings, reports (for map dots), backtest."""
    res = run(ds, p)
    bt = backtest(ds, res, p)
    payload = {
        "date": ds.date,
        "steps": res["steps"],
        "city": res["city"],
        "levels": {c: "".join(str(x) for x in s) for c, s in res["levels"].items() if any(s)},
        "reports": [
            {"t": r["t"], "lat": round(r["lat"], 5), "lon": round(r["lon"], 5), "category": r["category"], "address": r.get("address")}
            for r in ds.reports
            if r["category"] in FLOOD_CATS or r["category"] == "tree"
        ],
        "alerts": ds.alerts,
        "backtest": {k: v for k, v in bt.items() if k != "per_cell"},
        "per_cell": bt["per_cell"],
        "sources": ds.sources,
        "storm_days_before": ds.storm_days,
    }
    path = DATA_DIR / f"{ds.date}.timeline.json"
    path.write_text(json.dumps(payload, separators=(",", ":"), default=str))
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="highground.replay")
    ap.add_argument("cmd", choices=["prepare", "backtest", "summary", "calibrate", "publish"])
    ap.add_argument("date", nargs="+")
    ap.add_argument("--holdout", help="report this day separately, excluded from training")
    args = ap.parse_args(argv)
    if args.cmd == "calibrate":
        train = [(d := ReplayDataset.load(x), run(d)) for x in args.date if x != args.holdout]
        cal = calibrate(train)
        out = {"calibration": cal["best"], "train_days": cal["train_days"], "grid": cal["table"]}
        if args.holdout:
            hd = ReplayDataset.load(args.holdout)
            hb = backtest(hd, run(hd), thresholds=cal["best"]["thresholds"])
            out["holdout"] = {k: v for k, v in hb.items() if k != "per_cell"}
        (DATA_DIR / "calibration.json").write_text(json.dumps(out, indent=2, default=str))
        json.dump({k: v for k, v in out.items() if k != "grid"}, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return
    if args.cmd == "publish":
        for d in args.date:
            path = publish(ReplayDataset.load(d))
            print(json.dumps({"published": str(path)}))
        return
    args.date = args.date[0]
    if args.cmd == "prepare":
        from highground.es import ES

        ds = prepare(args.date, ES())
        path = ds.save()
        print(json.dumps({"saved": str(path), "reports": len(ds.reports), "alerts": len(ds.alerts), "cells": len(ds.profiles)}))
        return
    ds = ReplayDataset.load(args.date)
    res = run(ds)
    bt = backtest(ds, res)
    if args.cmd == "summary":
        bt = {k: v for k, v in bt.items() if k != "per_cell"}
    json.dump(bt, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
