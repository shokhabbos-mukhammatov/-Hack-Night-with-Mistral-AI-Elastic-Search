"""Apify actors, off the request path: official guides crawl (multilingual) and Reddit/news street signals.

Every run is capped with maxTotalChargeUsd. Results land in ES through ingest jobs; the app never waits on Apify.
"""

from datetime import UTC, datetime

import httpx

from highground.http import request

API = "https://api.apify.com/v2"
CRAWLER = "apify~website-content-crawler"
REDDIT = "apify~reddit-scraper"  # official actor, pay per post

# Official pages only. Translated SFPUC pages are verified to exist (/es/...); other languages are
# crawled when present, and the guides index records each page's real language.
GUIDE_START_URLS = [
    # all verified 200 on 2026-11-03
    "https://www.sfpuc.gov/learning/emergency-preparedness",
    "https://www.sfpuc.gov/learning/emergency-preparedness/flood-maps",
    "https://www.sfpuc.gov/es/learning/emergency-preparedness/flood-maps",
    "https://www.sfpuc.gov/folsomstorm",
    "https://adoptadrain.sfwater.org/about",
    "https://adoptadrain.sfwater.org/how-to-safely-clean-a-storm-drain",
    "https://www.sf.gov/san-francisco-floodplain-administration",
    "https://www.sf.gov/information--be-know-official-emergency-alerts",
    "https://www.sf72.org/hazard/flood",
]
SIGNAL_QUERIES = ["flooding", "flooded", "underwater", "tree down", "storm drain", "sinkhole"]
SIGNAL_SUBREDDITS = ["sanfrancisco", "bayarea"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def run_actor(http: httpx.Client, token: str, actor: str, actor_input: dict, max_usd: float, webhook_url: str | None = None) -> dict:
    """Start an actor run (async). If webhook_url is set, Apify calls us back when it finishes."""
    params = {"maxTotalChargeUsd": max_usd}
    if webhook_url:
        import base64
        import json

        hooks = [{"eventTypes": ["ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.FAILED"], "requestUrl": webhook_url}]
        params["webhooks"] = base64.b64encode(json.dumps(hooks).encode()).decode()
    resp = request(http, "POST", f"{API}/acts/{actor}/runs", params=params, json=actor_input, headers=_headers(token), timeout=60)
    return resp.json()["data"]


def dataset_items(http: httpx.Client, token: str, dataset_id: str, limit: int = 1000) -> list[dict]:
    resp = request(
        http, "GET", f"{API}/datasets/{dataset_id}/items", params={"clean": "true", "limit": limit}, headers=_headers(token), timeout=120
    )
    return resp.json()


def crawler_input(start_urls: list[str] | None = None, max_pages: int = 120) -> dict:
    return {
        "startUrls": [{"url": u} for u in (start_urls or GUIDE_START_URLS)],
        "maxCrawlPages": max_pages,
        "maxCrawlDepth": 1,
        "crawlerType": "playwright:adaptive",
        "saveMarkdown": True,
        "removeCookieWarnings": True,
        "includeUrlGlobs": [
            {"glob": "https://www.sfpuc.gov/**/emergency-preparedness/**"},
            {"glob": "https://www.sfpuc.gov/**/grants/**"},
            {"glob": "https://adoptadrain.sfwater.org/**"},
            {"glob": "https://www.sf72.org/**"},
        ],
    }


def reddit_input(time_filter: str = "day", max_per_community: int = 50) -> dict:
    """Input for apify/reddit-scraper (schema checked 2026-11-03): community-scoped searches, posts only."""
    return {
        "scrapeMode": "posts",
        "communityNames": SIGNAL_SUBREDDITS,
        "communityQueries": SIGNAL_QUERIES,
        "communitySort": "new",
        "communityTimeFilter": time_filter,
        "maxPostsPerCommunity": max_per_community,
        "maxCommentsPerPost": 0,
        "includeNsfw": False,
    }


def guide_docs(items: list[dict], now: datetime | None = None) -> list[dict]:
    """Pure: crawler items -> guides docs, one per page section (keeps chunks small and citable)."""
    now = now or datetime.now(UTC)
    docs = []
    for it in items:
        url = it.get("url") or (it.get("crawl") or {}).get("loadedUrl")
        text = it.get("markdown") or it.get("text") or ""
        if not url or len(text) < 200:
            continue
        meta = it.get("metadata") or {}
        lang = (meta.get("languageCode") or _lang_from_url(url)).split("-")[0].lower()
        for i, section in enumerate(_sections(text)):
            docs.append(
                {
                    "_id": f"web:{url}#{i}",
                    "url": url,
                    "lang": lang,
                    "title": meta.get("title") or url,
                    "section": section[:120].split("\n", 1)[0].strip("# "),
                    "body": section,
                    "content": section,
                    "source_type": "web",
                    "fetched_at": now.isoformat(),
                }
            )
    return docs


def signal_candidates(items: list[dict]) -> list[dict]:
    """Pure: Reddit items -> minimal records for Mistral extraction. Never keeps usernames."""
    out = []
    for it in items:
        if it.get("dataType") not in (None, "post") and it.get("type") not in (None, "post"):
            continue
        text = " ".join(filter(None, [it.get("title"), it.get("body") or it.get("text") or it.get("selftext")]))
        url = it.get("url") or it.get("permalink")
        created = it.get("createdAt") or it.get("created_utc") or it.get("created")
        if not text or not url or not created:
            continue
        out.append({"source": "reddit", "url": url, "published_at": created, "text": text[:1500]})
    return out


def _lang_from_url(url: str) -> str:
    for code in ("es", "zh-hant", "zh-hans", "fil", "vi", "ru", "ko", "ar"):
        if f"/{code}/" in url:
            return code
    return "en"


def _sections(md: str, max_chars: int = 1800) -> list[str]:
    parts, cur = [], ""
    for block in md.split("\n## "):
        block = block if not parts and not cur else "## " + block
        if len(cur) + len(block) > max_chars and cur:
            parts.append(cur.strip())
            cur = block
        else:
            cur += ("\n" if cur else "") + block
    if cur.strip():
        parts.append(cur.strip())
    return parts
