"""The risk engine: a pure function of Inputs. No I/O, no clock. Live, alerts, map, replay and backtest all use it.

score = S x H + C + coastal + trees
  S  susceptibility: 100-yr flood zone, sea-level-rise zone, storm-day report history around the cell
  H  hazard: rain vs SFPUC's capacity threshold (~1.3 in / 3 h, per the 2024 Civil Grand Jury) + NWS products
  C  confirmation: time-decayed flood reports / incidents / signals nearby (signals can't make it High alone)
  coastal: high tide vs minor-flood threshold + NWS coastal products, shoreline cells only
  trees: wind gusts on saturated soil + fallen-tree reports
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from highground.models import (
    Components,
    EventKind,
    Inputs,
    Level,
    NearbyEvent,
    NwsAlert,
    Reason,
    RiskState,
)

MM_PER_IN = 25.4
SFPUC_FLOOD_MAP = "https://www.sfpuc.gov/learning/emergency-preparedness/flood-maps"
CGJ_2024 = "https://www.kqed.org/science/1993278/san-franciscos-aging-infrastructure-ill-prepared-for-future-flooding-report-warns"
NOAA_TIDES = "https://tidesandcurrents.noaa.gov/stationhome.html?id=9414290"


@dataclass(frozen=True)
class RiskParams:
    design_3h_mm: float = 33.0  # ~1.3 in in 3 hours
    heavy_1h_mm: float = 15.0
    thresholds: tuple[float, float, float] = (1.0, 2.0, 3.0)  # elevated, high, severe
    nws_boost: dict = field(
        default_factory=lambda: {
            "Flash Flood Warning": 1.2,
            "Flood Warning": 1.0,
            "Flood Advisory": 0.5,
            "Flash Flood Watch": 0.3,
            "Flood Watch": 0.3,
        }
    )
    coastal_boost: dict = field(
        default_factory=lambda: {"Coastal Flood Warning": 1.2, "Coastal Flood Advisory": 0.6, "Coastal Flood Watch": 0.3}
    )
    wind_boost: dict = field(default_factory=lambda: {"High Wind Warning": 0.8, "Wind Advisory": 0.4, "High Wind Watch": 0.2})
    tide_minor_ft: float = 6.8  # SF tide gauge, MLLW: roughly where NWS coastal advisories begin
    event_window: timedelta = timedelta(hours=6)
    decay_min: float = 120.0
    event_weight: dict = field(
        default_factory=lambda: {
            EventKind.flood_report: 1.0,
            EventKind.sewer_report: 0.4,
            EventKind.road_incident: 0.8,
            EventKind.street_signal: 0.35,
        }
    )
    gust_tree_kmh: float = 55.0
    base_susceptibility: float = 0.35


DEFAULT = RiskParams()


def level_for(score: float, p: RiskParams = DEFAULT) -> Level:
    e, h, s = p.thresholds
    if score >= s:
        return Level.severe
    if score >= h:
        return Level.high
    if score >= e:
        return Level.elevated
    return Level.low


def active_alerts(alerts: list[NwsAlert], as_of: datetime) -> list[NwsAlert]:
    return [a for a in alerts if (a.starts is None or a.starts <= as_of) and (a.ends is None or as_of < a.ends)]


def _decay(ev: NearbyEvent, as_of: datetime, p: RiskParams) -> float:
    age_min = max((as_of - ev.at).total_seconds() / 60.0, 0.0)
    dist = 1.0 if ev.distance_m <= 150 else 0.6 if ev.distance_m <= 300 else 0.3
    return math.exp(-age_min / p.decay_min) * dist


def _fmt_in(mm: float) -> str:
    return f"{mm / MM_PER_IN:.2f} in"


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else ""


def assess(inputs: Inputs, p: RiskParams = DEFAULT) -> RiskState:
    as_of = inputs.as_of
    prof, z = inputs.profile, inputs.profile.zone
    reasons: list[Reason] = []

    # --- S: susceptibility
    s = p.base_susceptibility
    if z.in_100yr:
        s += 0.6
        reasons.append(
            Reason(
                code="zone_100yr",
                text="Inside SFPUC's 100-year storm flood risk zone (deep, contiguous flooding is likely in a 100-year storm).",
                source="SFPUC 100-Year Storm Flood Risk Map",
                source_url=SFPUC_FLOOD_MAP,
            )
        )
    if z.slr24:
        s += 0.15
        reasons.append(
            Reason(
                code="slr24",
                text="Inside the area flooded by a 100-year storm plus 24 inches of sea level rise (BCDC).",
                source="BCDC Adapting to Rising Tides via DataSF",
                source_url="https://data.sfgov.org/d/esku-ejgv",
            )
        )
    if prof.storm_days_5y > 0 and prof.storm_reports_5y > 0:
        rate = prof.storm_reports_5y / prof.storm_days_5y
        s += min(rate / 0.5, 1.0) * 0.5
        reasons.append(
            Reason(
                code="history",
                text=(
                    f"{prof.storm_reports_5y} flooding reports were made near here on {prof.storm_days_5y} big storm days "
                    "in the 5 years before this date."
                ),
                value=float(prof.storm_reports_5y),
                unit="reports",
                source="SF311 history (DataSF)",
                source_url="https://data.sfgov.org/d/vw6y-z8j6",
            )
        )
    s = min(s, 1.6)

    # --- H: rain + NWS flood products
    w = inputs.weather
    r1 = (w.rain_1h_mm or 0.0) if w else 0.0
    r3 = (w.rain_3h_mm or 0.0) if w else 0.0
    f3 = (w.rain_next3h_mm or 0.0) if w else 0.0
    h_rain = max(r1 / p.heavy_1h_mm, r3 / p.design_3h_mm, f3 / p.design_3h_mm)
    if r3 >= 2.5 or r1 >= 1.0:
        reasons.append(
            Reason(
                code="rain_obs",
                text=f"{_fmt_in(r3)} of rain in the last 3 hours ({_fmt_in(r1)} in the last hour).",
                value=round(r3, 1),
                unit="mm",
                source="Open-Meteo",
                as_of=w.as_of if w else None,
            )
        )
    if f3 >= 2.5:
        reasons.append(
            Reason(
                code="rain_fcst",
                text=f"{_fmt_in(f3)} of rain forecast in the next 3 hours.",
                value=round(f3, 1),
                unit="mm",
                source="Open-Meteo forecast",
                as_of=w.as_of if w else None,
            )
        )
    if max(r3, f3) >= p.design_3h_mm * 0.8:
        reasons.append(
            Reason(
                code="design_storm",
                text="That is close to or above ~1.3 in. in 3 hours, the amount SFPUC found can cause inland flooding.",
                value=round(max(r3, f3), 1),
                unit="mm",
                source="2024 SF Civil Grand Jury (SFPUC analysis)",
                source_url=CGJ_2024,
            )
        )
    active = active_alerts(inputs.alerts, as_of)
    h_nws = 0.0
    for a in active:
        b = p.nws_boost.get(a.event, 0.0)
        if b:
            h_nws = max(h_nws, b)
            reasons.append(_alert_reason(a))
    hazard = h_rain + h_nws

    # --- C: confirmation (time-decayed nearby events)
    window = [e for e in inputs.events if as_of - p.event_window <= e.at <= as_of]
    conf, sig_part, flood_events = 0.0, 0.0, []
    for e in window:
        wgt = p.event_weight.get(e.kind, 0.0)
        if not wgt:
            continue
        if e.kind == EventKind.road_incident and e.label and "flood" not in e.label.lower():
            wgt *= 0.3
        val = wgt * (e.confidence or 1.0) * (1.5 if e.verified_photo else 1.0) * _decay(e, as_of, p)
        conf += val
        if e.kind == EventKind.street_signal:
            sig_part += val
        flood_events.append(e)
    conf = min(conf, 3.0)
    sig_part = min(sig_part, conf)
    reports = [e for e in flood_events if e.kind == EventKind.flood_report]
    if reports:
        latest = max(r.at for r in reports)
        reasons.append(
            Reason(
                code="reports_nearby",
                text=(
                    f"{len(reports)} flooding report(s) to 311 within 300 m in the last "
                    f"{int(p.event_window.total_seconds() // 3600)} hours, the latest {int((as_of - latest).total_seconds() // 60)} min ago."
                ),
                value=float(len(reports)),
                unit="reports",
                source="SF311 (Open311)",
                source_url="https://mobile311.sfgov.org/",
                as_of=latest,
            )
        )
    incidents = [e for e in flood_events if e.kind == EventKind.road_incident]
    if incidents:
        reasons.append(
            Reason(
                code="road_incidents",
                text=f"{len(incidents)} CHP road-hazard incident(s) nearby: {incidents[0].label or 'hazard'}.",
                value=float(len(incidents)),
                unit="incidents",
                source="CHP live incidents",
                source_url="https://cad.chp.ca.gov/",
            )
        )
    signals = [e for e in flood_events if e.kind == EventKind.street_signal]
    if signals:
        reasons.append(
            Reason(
                code="street_signals",
                text=f"{len(signals)} unverified public post(s) mention flooding nearby.",
                value=float(len(signals)),
                unit="posts",
                source="Public posts (unverified)",
                source_url=signals[0].source_url,
            )
        )

    # --- coastal (shoreline cells only)
    coastal = 0.0
    if z.slr24 or z.shoreline:
        t = inputs.tide
        tide_part = 0.0
        if t and t.next_high_ft is not None and t.next_high_ft >= p.tide_minor_ft - 0.6:
            tide_part = (t.next_high_ft - (p.tide_minor_ft - 0.6)) / 0.6
            reasons.append(
                Reason(
                    code="high_tide",
                    text=f"High tide of {t.next_high_ft:.1f} ft at the SF tide gauge ({_hm(t.next_high_at)}).",
                    value=t.next_high_ft,
                    unit="ft MLLW",
                    source="NOAA CO-OPS 9414290",
                    source_url=NOAA_TIDES,
                    as_of=t.as_of,
                )
            )
        c_nws = 0.0
        for a in active:
            b = p.coastal_boost.get(a.event, 0.0)
            if b:
                c_nws = max(c_nws, b)
                reasons.append(_alert_reason(a))
        coastal = min(tide_part, 1.5) + c_nws

    # --- trees
    gust = max((w.gust_kmh or 0.0) if w else 0.0, (w.gust_next3h_kmh or 0.0) if w else 0.0)
    wind = max(0.0, min((gust - p.gust_tree_kmh) / 30.0, 1.5))
    soil = 1.3 if r3 >= 10 else 1.0
    tree_events = [e for e in window if e.kind == EventKind.tree_report]
    w_nws = max([p.wind_boost.get(a.event, 0.0) for a in active] or [0.0])
    trees = min(wind * soil + w_nws + 0.4 * sum(_decay(e, as_of, p) for e in tree_events), 2.0)
    if wind > 0:
        reasons.append(
            Reason(
                code="wind",
                text=f"Wind gusts up to {gust:.0f} km/h ({gust / 1.609:.0f} mph): higher risk of falling trees and limbs.",
                value=round(gust),
                unit="km/h",
                source="Open-Meteo",
                as_of=w.as_of if w else None,
            )
        )
    for a in active:
        if a.event in p.wind_boost:
            reasons.append(_alert_reason(a))
    if tree_events:
        reasons.append(
            Reason(
                code="trees_nearby",
                text=f"{len(tree_events)} fallen or damaged tree report(s) nearby in the last few hours.",
                value=float(len(tree_events)),
                unit="reports",
                source="SF311 (Open311)",
                source_url="https://mobile311.sfgov.org/",
            )
        )

    score = s * hazard + conf + coastal + trees
    high = p.thresholds[1]
    if score >= high and (score - sig_part) < high:  # unverified posts alone can't push a block to High
        score = high - 0.01
    lvl = level_for(score, p)
    return RiskState(
        cell=inputs.cell,
        level=lvl,
        score=round(score, 3),
        components=Components(
            susceptibility=round(s, 3), hazard=round(hazard, 3), confirmation=round(conf + coastal, 3), trees=round(trees, 3)
        ),
        reasons=_dedupe(reasons),
        as_of=as_of,
        inputs_hash=inputs_hash(inputs),
    )


def _alert_reason(a: NwsAlert) -> Reason:
    until = f" until {_hm(a.ends)}" if a.ends else ""
    return Reason(code=f"nws:{a.event}", text=f"NWS {a.event} in effect{until}.", source="National Weather Service", source_url=a.url)


def _dedupe(reasons: list[Reason]) -> list[Reason]:
    seen, out = set(), []
    for r in reasons:
        if r.code in seen:
            continue
        seen.add(r.code)
        out.append(r)
    return out


def inputs_hash(inputs: Inputs) -> str:
    """Stable fingerprint of what drove the score (rounded), used as a cache key for narratives."""
    w = inputs.weather
    key = {
        "z": inputs.profile.zone.model_dump(),
        "h": [inputs.profile.storm_reports_5y, inputs.profile.storm_days_5y],
        "w": [round(x or 0, 0) for x in ((w.rain_1h_mm, w.rain_3h_mm, w.rain_next3h_mm, w.gust_kmh) if w else ())],
        "a": sorted(a.event for a in active_alerts(inputs.alerts, inputs.as_of)),
        "e": sorted(f"{e.kind}:{e.at.strftime('%H')}" for e in inputs.events if e.at <= inputs.as_of),
        "t": round(inputs.tide.next_high_ft, 1) if inputs.tide and inputs.tide.next_high_ft else None,
    }
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- hysteresis (pure; used by the alert loop)


def smooth_level(prev: Level | None, new: Level, calm_runs: int, needed_calm: int = 2) -> tuple[Level, int]:
    """Rise immediately; fall only after `needed_calm` consecutive lower evaluations. Returns (level, calm_runs)."""
    if prev is None or new.rank >= prev.rank:
        return new, 0
    calm_runs += 1
    if calm_runs >= needed_calm:
        return new, 0
    return prev, calm_runs
