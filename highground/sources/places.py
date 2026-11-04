"""Geocoder corpus from the city's own records: EAS addresses (3mea-di5p) + street intersections (jfxm-zeee)."""

import re
from collections.abc import Iterator

import httpx

from highground.geo import cell_of, in_sf
from highground.http import get_json

ADDRESSES = "https://data.sf.gov/resource/3mea-di5p.json"
INTERSECTIONS = "https://data.sf.gov/resource/jfxm-zeee.json"
PAGE = 50_000

SUFFIXES = {
    "ST": "STREET",
    "AVE": "AVENUE",
    "BLVD": "BOULEVARD",
    "DR": "DRIVE",
    "WAY": "WAY",
    "TER": "TERRACE",
    "PL": "PLACE",
    "CT": "COURT",
    "LN": "LANE",
    "RD": "ROAD",
    "HWY": "HIGHWAY",
    "ALY": "ALLEY",
    "CIR": "CIRCLE",
}
_ORDINALS = {
    "1ST": "FIRST", "2ND": "SECOND", "3RD": "THIRD", "4TH": "FOURTH", "5TH": "FIFTH", "6TH": "SIXTH",
    "7TH": "SEVENTH", "8TH": "EIGHTH", "9TH": "NINTH", "10TH": "TENTH", "11TH": "ELEVENTH", "12TH": "TWELFTH",
}  # fmt: skip
_WORDS_TO_ORD = {v: k for k, v in _ORDINALS.items()}


def normalize_query(q: str) -> str:
    """Pure: '17th and Folsom' / '17th & folsom st.' / '17th y Folsom' -> '17TH & FOLSOM'."""
    s = q.upper().replace(".", " ").replace(",", " ")
    s = re.sub(r"\b(SAN FRANCISCO|SF|CA|CALIFORNIA|USA|\d{5})\b", " ", s)
    s = re.sub(r"\s+(AND|AT|Y|ET|和|与|@|/|\+)\s+", " & ", s)
    s = re.sub(r"\s*&\s*", " & ", s)
    return re.sub(r"\s+", " ", s).strip()


def street_core(name: str) -> str:
    """Pure: 'FOLSOM ST' -> 'FOLSOM', '03RD ST' -> '3RD', 'THIRD' -> '3RD'."""
    parts = name.upper().split()
    if parts and parts[-1] in SUFFIXES:
        parts = parts[:-1]
    parts = [re.sub(r"^0+(\d)", r"\1", p) for p in parts]
    parts = [_WORDS_TO_ORD.get(p, p) for p in parts]
    return " ".join(parts)


def address_docs(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_sf(lat, lon) or not r.get("address"):
            continue
        out.append(
            {
                "_id": f"eas:{r.get('eas_baseid') or r['address']}",
                "kind": "address",
                "name": r["address"],
                "alt": [r["address"].replace(r.get("street_type") or "~~", "").strip()],
                "location": {"lat": lat, "lon": lon},
                "h3_9": cell_of(lat, lon),
                "neighborhood": r.get("nhood"),
                "zip": r.get("zip_code"),
            }
        )
    return out


def intersection_docs(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        a, b = r.get("street_name_1"), r.get("street_name_2")
        if not a or not b or not in_sf(lat, lon):
            continue
        ca, cb = street_core(a), street_core(b)
        alts = {f"{ca} & {cb}", f"{cb} & {ca}"}
        for k, v in _ORDINALS.items():
            if ca == k:
                alts.add(f"{v} & {cb}")
            if cb == k:
                alts.add(f"{ca} & {v}")
        out.append(
            {
                "_id": f"x:{r.get('id') or r.get('cnn')}",
                "kind": "intersection",
                "name": f"{a} & {b}",
                "alt": sorted(alts),
                "location": {"lat": lat, "lon": lon},
                "h3_9": cell_of(lat, lon),
                "zip": r.get("zip_code"),
            }
        )
    return out


def iter_dataset(http: httpx.Client, url: str) -> Iterator[list[dict]]:
    offset = 0
    while True:
        rows = get_json(http, url, params={"$limit": PAGE, "$offset": offset, "$order": ":id"}, timeout=180)
        if rows:
            yield rows
        if len(rows) < PAGE:
            return
        offset += PAGE
