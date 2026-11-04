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

- **Elasticsearch** features used:
  - `geo_shape` and `geo_distance` queries
  - a **geo_match enrich** ingest pipeline that tags every 311 report with its flood zone and neighborhood
  - a **percolator** for alert subscriptions
  - **ES|QL** block history
  - a continuous **transform** (reports per cell per hour)
  - **`_mvt` vector tiles** for the map
  - **RRF hybrid retrieval** (BM25 + `semantic_text` through a **Mistral inference endpoint**)
  - `search_as_you_type` geocoding over the city's own 245k addresses and intersections
- **Mistral** features used:
  - function-calling agent with read-only tools
  - structured outputs (Pydantic)
  - vision for photo triage
  - OCR for SFPUC PDFs
  - Voxtral transcription and TTS
  - moderation
  - Batch
  - `mistral-embed` via Elastic
- **Code style:** a functional core (`risk.assess` is a pure function) with three small typed seams: `InputsProvider`, `Source` and `Channel`.

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
python -m highground.ingest guides       # curated guides (+ Mistral OCR of PDFs, + Apify crawl if a token is set)
```

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
4. Run the ingest commands above once against the Cloud project, using a one-off Railway shell or your laptop.
5. Run `python scripts/smoke.py https://<web>.up.railway.app`, then check `/status`.

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
