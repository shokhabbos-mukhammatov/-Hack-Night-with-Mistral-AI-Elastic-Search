"""NOAA CO-OPS tides at San Francisco (Fort Point, station 9414290)."""

from datetime import UTC, datetime

import httpx

from highground.http import get_json

API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATION = "9414290"
STATION_URL = f"https://tidesandcurrents.noaa.gov/stationhome.html?id={STATION}"


def _ts(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def parse_predictions(payload: dict) -> list[dict]:
    out = []
    for p in payload.get("predictions", []):
        ts = _ts(p["t"])
        out.append(
            {
                "_id": f"tide_pred:{ts.strftime('%Y%m%d%H%M')}",
                "@timestamp": ts.isoformat(),
                "kind": "tide_pred",
                "value": float(p["v"]),
                "unit": "ft_mllw",
                "tide_type": p.get("type"),
                "source": "noaa-coops",
            }
        )
    return out


def parse_observed(payload: dict, kind: str = "tide_obs") -> list[dict]:
    out = []
    for p in payload.get("data", []):
        if p.get("v") in (None, ""):
            continue
        ts = _ts(p["t"])
        out.append(
            {
                "_id": f"{kind}:{ts.strftime('%Y%m%d%H%M')}",
                "@timestamp": ts.isoformat(),
                "kind": kind,
                "value": float(p["v"]),
                "unit": "ft_mllw",
                "source": "noaa-coops",
            }
        )
    return out


class TideSource:
    name = "noaa-tides"
    index = "conditions"
    interval_s = 30 * 60
    overlap_s = 0
    pipeline = None

    def fetch(self, http: httpx.Client, since: datetime | None, now: datetime) -> list[dict]:
        base = {"station": STATION, "datum": "MLLW", "units": "english", "time_zone": "gmt", "format": "json"}
        pred = get_json(
            http, API, params={**base, "product": "predictions", "interval": "hilo", "begin_date": now.strftime("%Y%m%d"), "range": 48}
        )
        latest = get_json(http, API, params={**base, "product": "water_level", "date": "latest"})
        return parse_predictions(pred) + parse_observed(latest)


def archive_hourly(http: httpx.Client, begin: str, end: str) -> list[dict]:
    """Replay: observed hourly heights for YYYYMMDD..YYYYMMDD."""
    payload = get_json(
        http,
        API,
        params={
            "station": STATION,
            "datum": "MLLW",
            "units": "english",
            "time_zone": "gmt",
            "format": "json",
            "product": "hourly_height",
            "begin_date": begin,
            "end_date": end,
        },
    )
    return parse_observed(payload, kind="tide_obs")
