"""Open-Meteo hourly rain + wind gusts per SF weather point (live forecast, archive, historical forecast)."""

from datetime import UTC, datetime, timedelta

import httpx

from highground.geo import WEATHER_POINTS
from highground.http import get_json

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HIST_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
HOURLY = "precipitation,wind_gusts_10m"


def _params(**extra) -> dict:
    names = list(WEATHER_POINTS)
    return {
        "latitude": ",".join(str(WEATHER_POINTS[n][0]) for n in names),
        "longitude": ",".join(str(WEATHER_POINTS[n][1]) for n in names),
        "hourly": HOURLY,
        "timezone": "UTC",
        **extra,
    }


def parse_hourly(payload, now: datetime | None, kind_prefix: str = "wx", source: str = "open-meteo") -> list[dict]:
    """Pure: Open-Meteo multi-location response -> one doc per (point, hour)."""
    blocks = payload if isinstance(payload, list) else [payload]
    names = list(WEATHER_POINTS)
    docs = []
    for name, block in zip(names, blocks, strict=False):
        h = block["hourly"]
        for t, p, g in zip(h["time"], h["precipitation"], h.get("wind_gusts_10m") or [None] * len(h["time"]), strict=False):
            ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
            docs.append(
                {
                    "_id": f"{kind_prefix}:{name}:{ts.strftime('%Y%m%d%H')}",
                    "@timestamp": ts.isoformat(),
                    "kind": "weather",
                    "point": name,
                    # Open-Meteo precipitation at hour T = total of the preceding hour
                    "rain_mm": p,
                    "gust_kmh": g,
                    "is_forecast": bool(now and ts > now),
                    "source": source,
                }
            )
    return docs


class WeatherSource:
    name = "open-meteo"
    index = "conditions"
    interval_s = 15 * 60
    overlap_s = 0
    pipeline = None

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        payload = get_json(http, FORECAST, params=_params(past_days=1, forecast_days=2))
        docs = parse_hourly(payload, now)
        for d in docs:  # every poll refreshes the whole window; watermark is "now"
            d["fetched_at"] = now.isoformat()
        return docs


def archive(http: httpx.Client, start: str, end: str) -> tuple[list[dict], list[dict]]:
    """Replay inputs: observed reanalysis + what the forecast said, for [start, end] (YYYY-MM-DD).

    Falls back to the SFO airport rain gauge (IEM ASOS archive) when Open-Meteo is unavailable
    (e.g. its free daily quota); in that case there is no archived forecast, so next-3h rain is 0.
    """
    try:
        obs = parse_hourly(get_json(http, ARCHIVE, params=_params(start_date=start, end_date=end)), None, "wxa", "open-meteo-archive")
    except httpx.HTTPError:
        return asos_archive(http, start, end), []
    try:
        fc = parse_hourly(
            get_json(http, HIST_FORECAST, params=_params(start_date=start, end_date=end)), None, "wxf", "open-meteo-historical-forecast"
        )
    except httpx.HTTPError:
        fc = []
    return obs, fc


ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def parse_asos_csv(text: str, station: str = "SFO") -> list[dict]:
    """Pure: IEM ASOS hourly METARs (p01i inches, gust knots) -> per-point weather docs (same gauge for all points)."""
    docs = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        _, valid, p01i, gust = parts[:4]
        ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        hour = ts.replace(minute=0) + (timedelta(hours=1) if ts.minute >= 30 else timedelta(0))
        rain = float(p01i) * 25.4 if p01i else 0.0
        g = float(gust) * 1.852 if gust else None
        for name in WEATHER_POINTS:
            docs.append(
                {
                    "_id": f"wxasos:{name}:{hour.strftime('%Y%m%d%H')}",
                    "@timestamp": hour.isoformat(),
                    "kind": "weather",
                    "point": name,
                    "rain_mm": round(rain, 2),
                    "gust_kmh": g,
                    "is_forecast": False,
                    "source": f"iem-asos-{station.lower()}",
                }
            )
    return docs


def asos_archive(http: httpx.Client, start: str, end: str, station: str = "SFO") -> list[dict]:
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end) + timedelta(days=1)
    params = {
        "station": station, "data": ["p01i", "gust"], "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
        "missing": "empty", "trace": "0.0001", "report_type": "3",
        "year1": s.year, "month1": s.month, "day1": s.day, "year2": e.year, "month2": e.month, "day2": e.day,
    }  # fmt: skip
    resp = httpx.get(ASOS, params=params, timeout=60, headers={"User-Agent": "HighGround-SF/0.1"})
    resp.raise_for_status()
    return parse_asos_csv(resp.text, station)
