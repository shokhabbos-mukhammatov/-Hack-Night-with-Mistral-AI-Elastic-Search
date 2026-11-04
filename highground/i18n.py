"""Pre-translated fallback texts via the Mistral Batch API (cheap, offline), used when live Mistral is down.

    python -m highground.i18n submit          # starts one batch job for all languages -> prints job id
    python -m highground.i18n collect <job>   # downloads results -> data/i18n/<lang>.json
"""

import json
import sys
from functools import lru_cache
from pathlib import Path

from highground.fallback import URL  # noqa: F401  (kept so translators see link context)
from highground.mistral_client import mistral

DIR = Path(__file__).resolve().parent.parent / "data" / "i18n"
LANGS = {"es": "Spanish", "zh": "Traditional Chinese (San Francisco usage)", "tl": "Filipino", "vi": "Vietnamese", "ru": "Russian",
         "ko": "Korean", "ar": "Arabic", "fr": "French", "ja": "Japanese"}  # fmt: skip


def source_strings() -> dict[str, str]:
    """Every static English string the fallback path can show (action texts + fixed reason texts)."""
    from datetime import UTC, datetime

    from highground.fallback import actions_for
    from highground.models import Components, Level, Reason, RiskState, ZoneInfo

    out = {}
    for lvl in Level:
        codes = ["zone_100yr", "reports_nearby", "wind", "high_tide"]
        st = RiskState(cell="c", level=lvl, score=0, components=Components(), reasons=[Reason(code=c, text="", source="") for c in codes], as_of=datetime.now(UTC))
        for a in actions_for(st, ZoneInfo(in_100yr=True)):
            out[f"action.{a.code}"] = a.text
    out["reason.zone_100yr"] = "Inside SFPUC's 100-year storm flood risk zone (deep, contiguous flooding is likely in a 100-year storm)."
    out["reason.slr24"] = "Inside the area flooded by a 100-year storm plus 24 inches of sea level rise (BCDC)."
    return out


def submit() -> str:
    strings = source_strings()
    reqs = []
    for code, name in LANGS.items():
        reqs.append({
            "custom_id": code,
            "messages": [
                {"role": "system", "content": f"Translate each value into {name} for San Francisco residents. Plain language, keep numbers, times, "
                 "names of places, programs, phone numbers and URLs exactly. Return a JSON object with the same keys."},
                {"role": "user", "content": json.dumps(strings, ensure_ascii=False)},
            ],
        })  # fmt: skip
    return mistral().batch_chat(reqs)


def collect(job_id: str) -> dict:
    m = mistral()
    job = m._client.batch.jobs.get(job_id=job_id)
    if job.status != "SUCCESS":
        return {"status": job.status}
    raw = m._client.files.download(file_id=job.output_file).read().decode()
    DIR.mkdir(parents=True, exist_ok=True)
    written = {}
    for line in raw.splitlines():
        row = json.loads(line)
        lang = row["custom_id"]
        content = row["response"]["body"]["choices"][0]["message"]["content"]
        data = json.loads(content[content.find("{") : content.rfind("}") + 1])
        (DIR / f"{lang}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        written[lang] = len(data)
    return {"status": job.status, "written": written}


@lru_cache(maxsize=16)
def table(lang: str) -> dict:
    p = DIR / f"{(lang or 'en').split('-')[0]}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def tr(lang: str, key: str, default: str) -> str:
    return table(lang).get(key, default) if lang and not lang.startswith("en") else default


if __name__ == "__main__":
    if sys.argv[1:2] == ["submit"]:
        print(submit())
    elif sys.argv[1:2] == ["collect"]:
        print(json.dumps(collect(sys.argv[2])))
    else:
        print(__doc__)
