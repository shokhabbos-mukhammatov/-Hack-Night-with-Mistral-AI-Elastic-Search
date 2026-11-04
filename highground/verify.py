"""Receipts check (plain code, no AI): every number in generated text must appear in the evidence.

The LLM may rephrase and translate, but it may not introduce a number, time, amount or phone number that the
tools didn't return. Sentences with unsupported numbers are dropped. (Same idea as verify.ts in NightHacks.)
"""

import json
import re

_NUM = re.compile(r"(?<![\w.])(\$?\d{1,3}(?:[,\d]{0,12})(?:\.\d+)?)(?:\s?(%|percent|por ciento|in\b|inch\w*|pulgadas|mm|ft|feet|pies|km/h|mph))?")
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th|ª|º|a|o)$", re.I)
_SENT = re.compile(r"(?<=[.!?。！？])\s+|\n+")
ALWAYS_OK = {"911", "311", "888", "777", "888-777", "24", "7"}  # emergency/service numbers, "24/7"


def _numbers_in(text: str) -> set[float]:
    out = set()
    for m in _NUM.finditer(text):
        raw = m.group(1).replace("$", "").replace(",", "")
        try:
            out.add(round(float(raw), 2))
        except ValueError:
            continue
    return out


def evidence_numbers(*evidence) -> set[float]:
    nums: set[float] = set()
    for ev in evidence:
        text = ev if isinstance(ev, str) else json.dumps(ev, default=str)
        nums |= _numbers_in(text)
    # allow common unit conversions that a fluent rewrite may use
    extra = set()
    for n in nums:
        extra |= {round(n / 25.4, 2), round(n * 25.4, 1), round(n / 25.4, 1), round(n), round(n, 1), round(n * 0.621, 0), round(n / 0.621, 0)}
    return nums | extra


def _token_ok(token: str, ev: set[float]) -> bool:
    if token in ALWAYS_OK:
        return True
    try:
        v = round(float(token.replace("$", "").replace(",", "")), 2)
    except ValueError:
        return True
    if v in ev or round(v) in ev or round(v, 1) in ev:
        return True
    return any(abs(v - e) <= max(0.01, abs(e) * 0.02) for e in ev)


def check(text: str, *evidence) -> tuple[str, int, list[str]]:
    """Returns (kept_text, dropped_count, dropped_sentences)."""
    ev = evidence_numbers(*evidence)
    kept, dropped = [], []
    for sent in [s for s in _SENT.split(text) if s.strip()]:
        tokens = []
        for w in re.findall(r"\$?\d[\d,.:]*\w*", sent):
            if _ORDINAL.match(w) or ":" in w:  # street ordinals ("17th") and clock times handled below
                continue
            tokens.append(re.sub(r"[^\d.,$]", "", w).rstrip(".,"))
        times_ok = all(t in json.dumps(evidence, default=str) or _time_in_evidence(t, evidence) for t in re.findall(r"\b\d{1,2}:\d{2}\b", sent))
        if all(_token_ok(t, ev) for t in tokens if t) and times_ok:
            kept.append(sent.strip())
        else:
            dropped.append(sent.strip())
    return " ".join(kept), len(dropped), dropped


def _time_in_evidence(t: str, evidence) -> bool:
    blob = json.dumps(evidence, default=str)
    h, m = t.split(":")
    return f"{int(h):02d}:{m}" in blob or f"{int(h)}:{m}" in blob
