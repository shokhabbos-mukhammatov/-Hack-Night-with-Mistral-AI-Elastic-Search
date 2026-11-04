# HighGround SF

**Is my block going to flood or be dangerous in the next few hours, and what should I do?**

HighGround answers that for anyone in San Francisco, in their language. It combines:
- live SF311 reports
- National Weather Service alerts
- NOAA tides
- rain forecasts
- CHP road incidents
- SFPUC's 100-year flood map

It turns them into a block-level risk level, the reasons behind it, and concrete next steps. Every number links to its source. It also replays real storms to show how early it would have warned people.

Built with **Mistral AI** and **Elasticsearch** for the SF hack night ("make life in SF better").

## Why this matters

Full research with sources: [`docs/RESEARCH.md`](docs/RESEARCH.md).

- **About 32,000 flooding reports to 311 since 2008.** Since 2019, 27% of them came in on just 20 storm days. On Dec 31, 2022 there were 560 in one day.
- **Costs keep piling up:**
  - 2003–04 floods: about $5M in lawsuit payouts.
  - Dec 2014 storms: more than $6M in claims.
  - 2022–23 storms: $46M initial damage estimate. Most of the 117 residents who reported damage had no flood insurance.
  - 2023 windstorms: 3 people were killed by falling trees.
- **The warning existed but nobody acted on it at the block level.** NWS issued a Flood Watch about 96 hours ahead of Dec 31, 2022, yet the 17th & Folsom barriers weren't put up until days later.
- **311 is SF's only real-time flood sensor, and it lags.** About 30% of storm-day flood cases end as duplicates or "nothing found." SF has no public street-flood sensors; New York has 250+.

## What it does

| | |
|---|---|
| **Block briefing** | Enter an address or intersection, or use your location. You get a risk level (Low, Elevated, High or Severe) with the reasons: 100-yr flood zone, sea-level-rise zone, 5-year storm history, rain now and forecast, NWS alerts, tide, nearby 311 reports and CHP incidents. Then what to do and where to get help: sandbags, grants, insurance, Adopt-a-Drain. |
| **Ask** | Ask anything, in any language, by text or voice. Answers are grounded in official guidance and live data, with citations. |
| **Report a hazard** | Photo, voice or text becomes a structured 311 report, with a duplicate check first. You confirm, then it goes to SF311 (or directly through Open311 once we have a key). |
| **Alerts** | iMessage (Photon), Telegram or Web Push alerts when your block's risk rises or a hazard is reported near you. STOP works on every channel. |
| **Storm Replay** | Seven real storm days replayed hour by hour, using only data that existed at each moment. Includes a backtest of how early each flooded block was flagged. |

**How it stays trustworthy:**
- **The risk score is deterministic code.** Mistral explains it and translates it; it never invents the score.
- **A plain-code receipts check** drops any sentence whose numbers aren't in the source data.
- **It works without Mistral,** using template briefings in 6 languages, BM25 search and keyword intents.

### Backtest: would it have helped?

These are real archives: Open-Meteo reanalysis, plus the historical forecast models gave at the time, NOAA tides, IEM's archive of NWS products, and DataSF 311. Thresholds were calibrated on 6 storm days, and Dec 31, 2022 was held out as the test day.

| | Training: 6 storm days, 2023–25 | Test: Dec 31, 2022 (record storm) |
|---|---|---|
| Flooded blocks flagged High **before** their first 311 report (recall) | 0.43 | **0.69** (117 of 170) |
| Blocks flagged High that really flooded nearby (precision) | 0.67 | 0.67 |
| Median warning lead time | 93 min | **6.5 h** |

On Dec 31, 2022 a subscriber at 17th & Folsom would have gotten a High alert at **2:00 AM**. The first 311 flood report anywhere in the city came at 5:40 AM. To reproduce: `python -m highground.replay calibrate …` (see `data/replay/calibration.json`).

## Architecture

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
worker ──polls──▶ SF Open311 · NWS · NOAA · Open-Meteo · CHP · DataSF (nightly) · Apify (guides, signals)
   │                                   │
   └─ risk loop (all 1,137 H3 cells, 0.5 s) ─▶ Elasticsearch ◀── web (FastAPI + accessible PWA)
       alerts outbox ─▶ iMessage / Telegram / Web Push            │
                                                                   └─▶ Mistral (narration, Q&A tools, vision, OCR, Voxtral, moderation)
```

**Code style:** a functional core (`risk.assess` is a pure function) with three small typed seams: `InputsProvider`, `Source` and `Channel`.

### How HighGround uses Elastic

All of these are verified on Elasticsearch/Kibana 9.5.6 with real data unless marked *(needs ML/EIS)*.

| Capability | Where it's used |
|---|---|
| **Ingest pipeline + `geo_match` enrich** | Every 311 report is tagged with its flood zone, sea-level-rise zone and neighborhood at ingest time |
| **`geo_shape` / `geo_distance`** | Point-in-zone checks, and hazards near you |
| **`search_as_you_type` + fuzzy matching** | Geocoder over the city's 245k addresses and intersections ("Folsom y 17th", typos) |
| **ES\|QL** | Block storm history |
| **ES\|QL `LOOKUP JOIN`** | Ops hotspots: recent reports × the block's flood zone and storm history, joined from a lookup-mode index |
| **ES\|QL `COMPLETION`** | Neighborhood summaries written by Mistral inside the query (`mistral-completion` endpoint) |
| **`significant_terms`** | "Unusual right now": neighborhoods and report types over-represented compared with the last 30 days |
| **Continuous transform** | Reports per H3 cell per hour |
| **Percolator** | Alert subscriptions: each new hazard is matched against stored geo queries |
| **Vector tiles `_mvt`** | Map layers for points, risk cells and zones, plus **geohex aggregation tiles** for density |
| **Retrievers** | Guide search: `rule` → `text_similarity_reranker` *(Elastic Rerank, needs ML/EIS)* → `rrf` (BM25 + `semantic_text`) |
| **Synonyms API** | Updateable multilingual flood vocabulary, so "sandbags" = "sacos de arena" = "沙包" even without embeddings |
| **Query rules** | Emergency questions always pin the "call 911 / report to 311" guidance first, in the reader's language |
| **Inference API** | Mistral `text_embedding` (`semantic_text`), `chat_completion` and `completion` endpoints. Falls back to Elastic's multilingual E5 *(needs ML/EIS)*. |
| **Data streams + lifecycle** | Request logs (30-day retention) and risk history (400 days) |
| **Agent Builder + MCP** | 5 HighGround ES\|QL tools registered in Kibana (`scripts/agent_builder.py`) and served over Kibana's MCP server. The Mistral agent calls them through the Mistral SDK's MCP client (verified end to end). |

### How HighGround uses Mistral

Every path below is written against the official SDK. The ones marked † are unit-tested with a fake client and need a real key to verify.

| Capability | Where it's used |
|---|---|
| **Agents API** † | A persistent "HighGround SF" agent with our read-only ES tools, the built-in **`web_search`** (for live facts like a road closure, with cited sources) and **moderation guardrails** |
| **Conversations API** † | Memory per thread (web chat, Telegram, iMessage), so follow-ups like "and tonight?" keep context |
| **MCP** | The agent also gets Elastic Agent Builder tools over MCP (`elastic_highground_*`) |
| **Structured outputs** † | Briefings, 311 drafts, photo checks, report classification, signal extraction |
| **Vision** † | Triage of residents' photos, plus checking photos attached to live 311 reports (SSRF-allowlisted). Verified water weighs 1.5× in the risk engine. |
| **Mistral Small classification** † | Open311 lumps flooding, sewage and leaks into one category, so Mistral reads the description and sets it (category ids it invents are rejected) |
| **OCR** † | SFPUC PDFs (flood insurance guide) into the guides index, with page citations |
| **Voxtral** † | Voice questions and reports (transcription), and read-aloud (TTS) with a browser fallback |
| **Moderation** † | All user text and photos |
| **Batch API** † | Pre-translates every fallback text into 9 languages (`python -m highground.i18n submit`) for when live Mistral is down |
| **Embeddings** † | `mistral-embed` through the Elastic inference endpoint for `semantic_text` |

Every Mistral output goes through `verify.py`: a sentence with a number that isn't in the tool data is dropped. Answers backed by `web_search` keep their citations and are labeled.

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in what you have; everything optional degrades gracefully
```

You need an Elasticsearch: an Elastic Cloud Serverless project, or locally a single-node 9.x with security off for development. Then load data, about 3 minutes plus the backfill:

```bash
python -m highground.ingest setup        # indices + Mistral inference endpoints (if HG_MISTRAL_API_KEY)
python -m highground.ingest layers       # SFPUC flood zone, SLR layer, neighborhoods -> enrich policy + pipeline
python -m highground.ingest places       # 245k EAS addresses + intersections for geocoding
python -m highground.ingest backfill --years 5
python -m highground.ingest poll         # one pass of every live feed
python -m highground.ingest transform
python -m highground.ingest profiles     # per-cell susceptibility
python -m highground.ingest guides       # curated guides, synonyms, query rules (+ Mistral OCR of PDFs, + Apify crawl)
python scripts/agent_builder.py          # optional: Kibana Agent Builder ES|QL tools + MCP (HG_KIBANA_URL)
```

After upgrading, `python -m highground.ingest migrate` rebuilds the derived indices whose settings changed.

Then start the app and the worker:

```bash
uvicorn app:app --port 8000              # http://localhost:8000
python worker.py                         # pollers, risk loop, alerts, nightly sync
```

Tests and checks:

```bash
pytest -q                                # unit + integration (integration needs HG_ES_URL)
python scripts/smoke.py http://localhost:8000
node tests/e2e/a11y_and_flows.js /tmp http://localhost:8000   # Playwright + axe-core
```

Replay data is committed in `data/replay/`. To regenerate it:

```bash
python -m highground.replay prepare 2022-12-31
python -m highground.replay publish 2022-12-31
```

## Deploy (Railway + Elastic Cloud)

1. **Elastic Cloud**: create a Serverless project (Search). Copy the endpoint into `HG_ES_URL` and an API key into `HG_ES_API_KEY`.
2. **Railway**: create two services from this repo. Both use the `Dockerfile`.
   - `web`: config file `railway.web.json`. It health-checks `/healthz` and runs 2 replicas.
   - `worker`: config file `railway.worker.json`, which runs `python worker.py` as 1 replica, guarded by an ES lease lock.
3. Set the variables from `.env.example` on both services:
   - required: `HG_ES_URL`, `HG_ES_API_KEY`, `HG_MISTRAL_API_KEY`, `HG_FERNET_KEY`
   - `HG_BASE_URL`: the public web URL
   - Telegram: `HG_TELEGRAM_BOT_TOKEN` and `HG_TELEGRAM_WEBHOOK_SECRET` (the worker registers the webhook)
   - Photon: `HG_PHOTON_PROJECT_ID` and `HG_PHOTON_API_KEY`
   - Web Push: run `python -m highground.messaging.webpush --generate` for `HG_VAPID_*`
   - optional: `HG_APIFY_TOKEN`, and `HG_OPEN311_API_KEY` once SF311 issues one
   - Agent Builder: `HG_KIBANA_URL` (the Serverless project's Kibana URL) and `HG_KIBANA_API_KEY`
4. Run the ingest commands above once against the Cloud project, using a one-off Railway shell or your laptop.
5. Run `python scripts/agent_builder.py` once against the project's Kibana.
6. Run `python scripts/smoke.py https://<web>.up.railway.app`, then check `/status` and `/ops`.

Still pending on outside parties:
- **SF311 Open311 POST key:** request one at mobile311.sfgov.org/open311. Until then, reports hand off to sf311.org.
- **SMS:** needs 10DLC registration, which takes days. iMessage, Telegram and Web Push work today.

## Honest limitations

- HighGround is **not an official alert service**. The app always points people to 911, AlertSF and NWS.
- **No flood depths.** SFPUC's flood-elevation layer is a water-surface elevation in SF City Datum, not depth above ground.
- 311 counts reflect how often people report as well as how often it floods. Some of the growth in reports comes from wider 311 app use.
- Rain comes from model reanalysis and forecasts at 5 points across SF, not from street gauges.
- Without a Mistral key, curated guides exist in English, Spanish and Chinese. Briefing details stay in English with localized headlines and labels.

## Repo map

```
app.py                web service      worker.py            schedulers + Photon listener
highground/risk.py    pure risk engine highground/replay.py  replay, backtest, calibration
highground/sources/   one module per feed (+ shared runner in base.py)
highground/es.py      mappings, enrich, pipeline, transform, Store, locks
highground/agent.py   Mistral narration + tool loop      highground/verify.py  receipts check
highground/alerts.py  percolator, outbox, dispatcher     highground/messaging/ Photon · Telegram · Web Push
web/                  accessible PWA (MapLibre + text twin)  data/replay/  real archived storm days
docs/                 research + architecture            tests/        unit, integration, e2e
```
