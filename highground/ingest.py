"""CLI: one-time setup and loads, plus single poll runs (the worker schedules the same functions).

    python -m highground.ingest setup          # indices + Mistral inference endpoints
    python -m highground.ingest layers         # flood zones + neighborhoods, then enrich policy + pipeline
    python -m highground.ingest places         # geocoder corpus (EAS addresses + intersections)
    python -m highground.ingest backfill --years 5
    python -m highground.ingest poll [open311 nws-alerts open-meteo noaa-tides chp datasf]
    python -m highground.ingest profiles       # per-cell susceptibility (zones + 5y storm history)
    python -m highground.ingest transform      # continuous per-cell hourly pivot
    python -m highground.ingest guides         # Apify crawl -> guides (or --local data/guides/*.md)
    python -m highground.ingest all            # everything above, in order
"""

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from highground import es as es_mod
from highground import http as http_mod
from highground.sources import datasf, layers, places
from highground.sources.base import run_source
from highground.sources.chp import ChpSource
from highground.sources.hazards import merge_case
from highground.sources.noaa import TideSource
from highground.sources.nws import NwsAlertSource
from highground.sources.open311 import Open311Source
from highground.sources.weather import WeatherSource

log = logging.getLogger("highground.ingest")

SOURCES = {
    s.name: s
    for s in (Open311Source(), NwsAlertSource(), WeatherSource(), TideSource(), ChpSource(), datasf.DataSFSource())
}


def cmd_setup(es: es_mod.ES) -> dict:
    return es_mod.setup(es)


def cmd_layers(es: es_mod.ES) -> dict:
    out = {}
    with http_mod.client(timeout=180) as http:
        for layer, meta in layers.LAYERS.items():
            docs = layers.zone_docs(layer, layers.geojson(http, meta["dataset"]))
            out[layer] = es_mod.iter_bulk(es, "flood-zones", docs, batch=50)
        out["neighborhood"] = es_mod.iter_bulk(
            es, "flood-zones", layers.neighborhood_docs(layers.geojson(http, layers.NEIGHBORHOODS)), batch=20
        )
    es.client.indices.refresh(index=es.ix("flood-zones"))
    out["pipeline"] = es_mod.ensure_enrich_and_pipeline(es)
    return out


def cmd_places(es: es_mod.ES) -> dict:
    out = {"intersection": 0, "address": 0}
    with http_mod.client(timeout=180) as http:
        for rows in places.iter_dataset(http, places.INTERSECTIONS):
            out["intersection"] += es.upsert_docs("places", places.intersection_docs(rows))
        for rows in places.iter_dataset(http, places.ADDRESSES):
            out["address"] += es.upsert_docs("places", places.address_docs(rows))
    return out


def cmd_backfill(es: es_mod.ES, years: float, start: str | None = None, end: str | None = None) -> dict:
    now = datetime.now(UTC)
    s = datetime.fromisoformat(start).replace(tzinfo=UTC) if start else now - timedelta(days=int(365.25 * years))
    e = datetime.fromisoformat(end).replace(tzinfo=UTC) if end else None
    with http_mod.client(timeout=180) as http:
        n = es_mod.iter_bulk(es, "hazard-reports", datasf.backfill(http, s, e), pipeline=es_mod.PIPELINE, merge_fn=merge_case)
    es.save_status("datasf", {**(es.get_status("datasf") or {}), "name": "datasf", "interval_s": 86400,
                              "last_success": now.isoformat(), "watermark": now.isoformat(), "consecutive_failures": 0})  # fmt: skip
    return {"hazard_reports": n}


def cmd_poll(es: es_mod.ES, names: list[str]) -> list[dict]:
    results = []
    with http_mod.client() as http:
        for name in names or [n for n in SOURCES if n != "datasf"]:
            r = run_source(SOURCES[name], http, es)
            results.append(r.__dict__)
    return results


def cmd_profiles(es: es_mod.ES) -> dict:
    from highground.profiles import build_profiles

    return build_profiles(es, datetime.now(UTC))


def cmd_transform(es: es_mod.ES) -> dict:
    return {"transform": es_mod.ensure_transform(es)}


def cmd_guides(es: es_mod.ES, local: bool) -> dict:
    from highground.guides import load_guides

    return load_guides(es, local_only=local)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="highground.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("setup", "layers", "places", "profiles", "transform", "all"):
        sub.add_parser(c)
    b = sub.add_parser("backfill")
    b.add_argument("--years", type=float, default=5)
    b.add_argument("--start")
    b.add_argument("--end")
    p = sub.add_parser("poll")
    p.add_argument("names", nargs="*", help=f"any of: {', '.join(SOURCES)}")
    g = sub.add_parser("guides")
    g.add_argument("--local", action="store_true", help="only load data/guides/*.md (no Apify)")
    args = ap.parse_args(argv)
    es = es_mod.ES()
    if args.cmd == "all":
        result = {
            "setup": cmd_setup(es),
            "layers": cmd_layers(es),
            "places": cmd_places(es),
            "backfill": cmd_backfill(es, 5),
            "poll": cmd_poll(es, []),
            "transform": cmd_transform(es),
            "profiles": cmd_profiles(es),
            "guides": cmd_guides(es, local=not es.cfg.apify_token),
        }
    elif args.cmd == "setup":
        result = cmd_setup(es)
    elif args.cmd == "layers":
        result = cmd_layers(es)
    elif args.cmd == "places":
        result = cmd_places(es)
    elif args.cmd == "backfill":
        result = cmd_backfill(es, args.years, args.start, args.end)
    elif args.cmd == "poll":
        result = cmd_poll(es, args.names)
    elif args.cmd == "profiles":
        result = cmd_profiles(es)
    elif args.cmd == "transform":
        result = cmd_transform(es)
    elif args.cmd == "guides":
        result = cmd_guides(es, args.local)
    else:  # pragma: no cover
        ap.error("unknown command")
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
