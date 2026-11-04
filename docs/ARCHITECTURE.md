# HighGround SF: system design

## Requirements

**Functional:**
- **F1 Briefing:** for an address or point, return the risk level with its reasons, actions, nearby hazards and storm history, in any language.
- **F2 Ask:** Q&A grounded in official guides and live tools, with citations.
- **F3 Report:** photo, voice or text goes through moderation and classification. The app checks for duplicates nearby, drafts the 311 report, then hands it to SF311.
- **F4 Alerts:** subscribe by address, radius or route over iMessage, Telegram or Web Push. Alerts are sent when risk rises or a new hazard appears nearby, and STOP works on every channel.
- **F5 Map:** live layers for reports, zones, risk cells and incidents, plus a text-list twin for every layer.
- **F6 Replay and backtest:** reconstruct any archived storm day hour by hour, with no look-ahead, and measure lead time and false alarms.
- **F7 Status:** show how fresh each data feed is, publicly.

**Non-functional** (assumptions are explicit, and they get revisited after launch):

| | Target |
|---|---|
| Load | About 500 daily users normally. A storm-day spike to about 20k users and about 20 requests/s for briefings at peak. About 1k–10k alert subscribers. |
| Latency (p95) | Cached briefing under 1.5 s. Uncached under 6 s, streaming text after about 1.5 s. Tiles under 300 ms. Ask under 8 s. |
| Freshness | Open311 within 5 min. NWS within 5 min. CHP within 5 min. Rain within 15 min. Tides within 30 min. Risk cells recomputed every 5 min, and every 2 min while an NWS flood product is active. |
| Availability | 99.5% during storm season. The briefing must keep working without Mistral, using the template fallback. |
| Alert delivery | Within 2 minutes of a risk change, with at most one alert per subscriber per block every 3 hours unless the level goes up. |
| Cost | Mistral spend is bounded by caching (4.4). Apify is capped at about $20. ES Serverless stays small (well under 1 GB of data). |
| Accessibility and privacy | WCAG 2.2 AA. Minimal personal data, with deletion on STOP. |

**Constraints:**
- Built in one hack night by a team of 1–4, then kept running.
- Required stack: Mistral, Elastic Cloud Serverless, Python, Railway.
- Upstream feeds offer only polling, no push.

## High-level design

A **modular monolith**: one codebase and one container image, running as two process types.

```
                       ┌─────────────────────────── Railway ───────────────────────────┐
 Residents ──HTTPS──▶  │ web (FastAPI, 2+ replicas, stateless)                          │
 (PWA, screen readers, │   /api/* · /tiles/* · static PWA · /webhooks/{telegram,apify}  │
  voice)               │   request path touches ONLY: Elasticsearch + Mistral + geocoder│
                       │                                                                │
 iMessage ◀─Photon──▶  │ worker (1 replica + ES lease lock)                             │
 Telegram ◀─Bot API──▶ │   schedulers: pollers · nightly sync · risk engine · signals   │
 Web Push ◀─VAPID────  │   dispatcher: alerts-outbox → channels                         │
                       │   Photon Socket.IO listener (inbound texts)                    │
                       └───────────────┬───────────────────────────────┬────────────────┘
                                       │                               │
                          ┌────────────▼────────────┐       ┌──────────▼──────────┐
                          │ Elastic Cloud Serverless│       │ Mistral API         │
                          │ indices · inference     │◀─────▶│ chat/tools · vision │
                          │ (mistral-embed, chat)   │       │ OCR · Voxtral · mod │
                          │ transforms · enrich     │       │ batch · embeddings  │
                          └────────────▲────────────┘       └─────────────────────┘
                                       │ upserts (idempotent)
   Upstreams (polled by worker only): SF Open311 · DataSF · NWS · NOAA · Open-Meteo · CHP · IEM
   Apify (scheduled actors → webhook → worker job): guides crawl · Reddit/news signals
```

**Key design rules:**
1. **The request path never calls upstream feeds.** The worker writes everything into ES, and the web process reads only from ES, plus Mistral and the cached geocoder. Upstream outages or rate limits never reach users.
2. **The risk engine is a pure function:** `risk(cell, as_of, inputs) → RiskState`. The live loop, briefings, alerts, map colors, replay and backtest all call the same code. Two benefits:
   - The replay proves what the live system would have done.
   - It's straightforward to unit test.
3. **A "block" is an H3 cell at resolution 9** (about 0.1 km², around 1,200 cells covering SF). Using cells keeps caching and alert matching cheap. Point-exact checks (flood zone, distances) still use the actual coordinates.
4. **The LLM explains and never decides.** Risk scores, levels and actions come from code. Mistral writes the wording, translates it, and handles unstructured input.
5. **Everything is idempotent:**
   - Deterministic `_id`s (`open311:{id}`, `chp:{log_id}`, `outbox:{sub}:{cell}:{level}:{window}`)
   - `op_type=create` where duplicates must be blocked
   - Overlapping poll windows

## Data flows

**A. Ingest and risk loop (worker):**
```
every 2m  Open311 GET ?service_code=PUC:Water:Sewer|PW:BUF:Tree Maintenance|input:Pothole & Street Issues
          &start_date=watermark-15m&page_size=200 (paginate until < watermark) → normalize
          → Mistral Small classify description (flooding vs sewer vs water-leak; Open311 lacks subtype)
          → [photo? → vision "water visible?" → confidence] → ES ingest pipeline (enrich geo_match: zone, SLR, neighborhood;
          set h3_9) → upsert hazard-reports
every 5m  NWS alerts/hourly · CHP GGCC (SF filter) → conditions / road-incidents
every 15m Open-Meteo now+forecast; every 30m NOAA tides (obs+pred) → conditions
nightly   DataSF 311 sync (06:00 PT dataset refresh → run 07:00) → upsert history, close statuses
every 5m (2m during NWS flood products) risk engine over active cells (cells with subscribers ∪ cells with
          any signal in last 6h ∪ all 100-yr-zone cells) → risk-cells (latest) + risk-history (data stream)
          → level increased? → alert trigger (B)
```

**B. Alert matching and dispatch:**
```
trigger 1 (risk change in cell X):   subscriptions where cells contains X and min_level <= new level
trigger 2 (new hazard event point):  percolate event doc against subscriptions' geo_distance / geo_shape
                                     queries (address radius or commute route polygon)
→ hysteresis + quiet-hours rules → Mistral Small writes ≤320-char alert in subscriber's lang (template
  fallback) → alerts-outbox (_id = idempotency key, op_type=create) → dispatcher every 15s:
  send via Photon / Telegram / Web Push → status sent | retry (backoff 30s→1h, max 5) | dead-letter
```

**C. Briefing request (web):**
```
GET /api/briefing?address=… → geocode (ES geocode-cache → US Census geocoder) → h3 cell
→ parallel ES: risk-cells[X] · flood-zones geo_shape(point) · hazard-reports geo_distance 300m last 6h
  · road-incidents & street-signals nearby · conditions latest · block-hourly history (ES|QL)
→ if risk-cells[X].computed_at older than its newest input → recompute inline (same pure function)
→ narrative cache key (h3, lang, risk_state_hash): hit → return | miss → Mistral Large (structured output,
  ≤8s timeout) → verify.py receipts check → cache 10 min → return (stream text; template on failure)
```

**D. Report (web):**
```
POST /api/report (photo|audio|text, lat/lon) → moderation (Moderation 2 / Shieldstral; image) → Voxtral
transcribe if audio → vision classify → duplicates: hazard-reports within 75 m / 3 h same category
→ structured 311 draft (service_code, description, address) → user confirms → Open311 POST (when key)
  or SF311 handoff link → poller later sees it → loop closed. Photo kept in memory only.
```

**E. Inbound text** (Photon Socket.IO or Telegram webhook):
```
"alerta para 17th y Folsom" → Mistral Small intent (subscribe | unsubscribe | ask | report) + lang
→ geocode → create subscription (pending) → confirmation reply → "YES" → active. STOP anywhere → delete.
"ask" intents reuse flow C/F2 and reply with the short-form briefing.
```

**F. Replay and backtest:**
```
InputsProvider interface: Live(ES) | Archive(date) [Open-Meteo archive + historical-forecast, NOAA
hourly_height, IEM VTEC, DataSF 311 by requested_datetime, Reddit/news track]. Archive inputs are
materialized into replay-<date> indices once (cached). Replay = risk(cell, t, Archive) every 15 min.
Backtest = for each cell with ≥1 report that day: lead_time = first t where level ≥ High − first report
within k-ring(1); false alarms = cells at ≥ High with no report within 6 h. Results stored in backtests.
```

## Deep dive

**Data model: ES indices.**
All indices use explicit mappings, keyword fields for filters, and `h3_9` on every geo document.

| Index | Key fields | Notes |
|---|---|---|
| `hazard-reports` | `_id` = `311:<case_id>` (Open311 and DataSF merge into one doc), category, subcategory, status, requested_at, updated_at, closed_at, `location` (geo_point), address, neighborhood, h3_9, description, media_url, vision {water_visible, conf}, zone_100yr, slr24 | Ingest pipeline with **enrich geo_match** against `flood-zones`. About 150k docs. |
| `flood-zones` | `geometry` (geo_shape), layer (100yr, slr24, neighborhood), name, class, dataset_id | Source for the enrich policy, re-executed when layers change |
| `conditions` (data stream) | @timestamp, kind (rain_obs, rain_fcst, tide_obs, tide_pred, nws_alert), value, unit, valid_from, valid_to, event, severity, area, source | Time series inputs to the risk engine |
| `road-incidents` | `_id` = CHP log id, type, location, details[], started_at, updated_at | SF-filtered |
| `street-signals` | url, source, published_at, excerpt (≤280 chars), location, h3_9, hazard_type, confidence, moderation | **No author fields** |
| `guides` | url, lang, title, section, `content` (semantic_text → Mistral embed endpoint), `body` (text/BM25), source_type, page, fetched_at | RRF retriever |
| `risk-cells` | `_id` = h3, level, score, components {susceptibility, hazard, confirmation}, reasons[] {code, value, source, as_of}, inputs_hash, computed_at, centroid, polygon (geo_shape) | Latest state; `_mvt` risk layer |
| `risk-history` (data stream) | the same fields plus @timestamp | Audit, charts, comparison with replays |
| `block-hourly` | h3_9, hour, counts by category | **Continuous transform** from hazard-reports |
| `subscriptions` | `_id`, channel, `handle_enc` (Fernet-encrypted phone or chat id), handle_hash, lang, cells[], `query` (**percolator**), min_level, status, created_at, confirmed_at, last_alert {cell, level, at} | Least-privilege API key |
| `alerts-outbox` | `_id` = idempotency key, sub_id, channel, payload, status, attempts, next_attempt_at, sent_at, error | Outbox pattern |
| `narratives` | `_id` = h3 + lang + state hash, text, verified, expires_at | L2 cache |
| `geocode-cache`, `locks`, `jobs`, `backtests`, `replay-*`, `logs-highground` (data stream) | | Supporting indices |

**API contracts** (JSON; documented with OpenAPI from FastAPI and Pydantic models):
```
GET  /api/briefing?address=|lat=&lon= &lang=auto
  → { cell, location{lat,lon,address}, risk{level: low|elevated|high|severe, score, components},
      reasons[{code, text, value, unit, source, source_url, as_of}], actions[{text, url}],
      zone{in_100yr, slr24}, nearby{reports[], incidents[], signals[]},
      history{reports_5y, storm_days, top_dates[]}, conditions{alerts[], rain{last1h,last3h,next3h},
      tide{next_high_ft, at}}, narrative{text, lang, verified, dropped_claims}, freshness{feed: age_s} }
POST /api/ask        {question, lang?, lat?, lon?, conversation_id?} → {answer, citations[{url,title}], lang}
POST /api/report     multipart{photo?|audio?|text?, lat, lon} → {report_id, moderation, category,
                     duplicates[], draft{service_code, description, address}, handoff_url}
POST /api/report/{id}/submit → {status, service_request_id?}        (enabled when Open311 key present)
POST /api/subscribe  {channel, address|lat/lon, radius_m?, route_geojson?, lang, min_level, push_sub?}
                     → {subscription_id, status: pending|active}
DELETE /api/subscribe/{id}?token= → 204
GET  /tiles/{reports|risk|zones}/{z}/{x}/{y}.pbf  (ES _mvt; Cache-Control 60s)
GET  /api/list/{layer}?lat&lon&radius   (text-list twin of every map layer)
GET  /api/replay/{date}/timeline · /api/replay/{date}?t=ISO · /api/backtest/{date}
POST /webhooks/telegram · /webhooks/apify   (secret-verified, ack fast, enqueue job)
GET  /healthz · /status
```

**Mistral agent (F2 and narratives):**
- **Tools:** `geocode`, `get_briefing`, `nearby_reports`, `block_history`, `live_conditions`, `search_guides`. They are **read-only**.
- The agent **can't** send messages or submit to 311; those require explicit user actions in the UI. This limits the damage a prompt injection hidden in guides or street signals could do. Tool outputs are passed as data, inside delimiters.
- A maximum of 4 tool rounds.
- The structured output schema is validated with Pydantic. On failure it re-asks once, then falls back to the template.
- `verify.py` pulls numbers, times and place names from the text. Any sentence whose facts don't appear in the tool JSON or a cited guide chunk is dropped. If more than 2 sentences are dropped, the template is used instead.

**Risk engine** (`risk.py`):
- `score = S × H + C`, where:
  - **S (susceptibility):** 100-yr zone, SLR zone, and the cell's 5-year storm-day report rate.
  - **H (hazard):** observed rain over 1 and 3 hours, forecast rain for the next 3 hours, the NWS product (Flood Advisory, Watch or Warning; Coastal Flood), and for shoreline cells, the tide relative to its threshold.
  - **C (confirmation):** time-decayed count of reports, CHP incidents and verified photos nearby. Street signals carry less weight and can't raise a cell to High on their own.
- **Levels** come from thresholds calibrated on the top 10 storm days to maximize lead time, with fewer than about 10% false alarms.
- **Hysteresis:** the level goes up immediately, but comes down only after 2 consecutive calm evaluations.

**Caching:**
| Layer | Key | TTL / invalidation |
|---|---|---|
| Narrative (in-process LRU, then ES `narratives`) | h3 + lang + risk_state_hash | 10 min, or immediately when the state hash changes |
| Geocode (ES) | normalized address | 30 days |
| Risk cells | h3 | Recomputed by the worker; inline recompute if stale |
| Tiles | z/x/y + layer | `Cache-Control: max-age=60` |
| Static PWA | hashed filenames + service worker | The service worker also caches the last briefing for offline viewing |

The cache key bounds Mistral cost by the **number of active cells × languages**, about 1.2k × 5 at the extreme. It doesn't grow with the number of users.

**Error handling and retries:**
| Dependency | Policy |
|---|---|
| Upstream feeds | Per-source timeout of 10 s. Exponential backoff with jitter. A circuit breaker opens after 3 failures, shows the feed as stale on `/status` and the UI, and retries after 5 minutes. Overlapping windows mean retries never leave gaps. |
| Mistral | Timeouts of 8 s for briefings, 15 s for vision, and 30 s for Batch and OCR. One retry on 429 or 5xx. Then the template fallback for briefings and alerts; for reports, the user picks the category manually. |
| Elasticsearch | A hard dependency. Retry reads once. If it's still down, serve a static degraded page that links to AlertSF, SF311 and NWS, plus the last risk snapshot held in memory. |
| Channels | Photon, Telegram and Push errors are classified: a 4xx for an invalid handle marks the subscription invalid; 429 or 5xx is retried through the outbox; failures after 5 attempts go to a dead-letter queue and are counted on the dashboard. |
| Webhooks | Verify the signature or secret, return 200 within 100 ms, and process the job in the worker. Deterministic job IDs prevent double processing. |

## Scale and reliability
- **Load math:**
  - At 20 requests/s with an 80% narrative cache hit rate, that's about 4 uncached LLM calls per second, which needs a Mistral concurrency limit of about 30 and a queue. ES sees about 120 queries/s, which is easy for Serverless.
  - The ingest peak (Dec 31, 2022) was 130 flood reports an hour, so a 2-minute poll sees fewer than 10 rows. That's trivial.
- **Scaling:**
  - Web scales out horizontally with no state; it runs 2 replicas by default.
  - The worker scales up instead and stays a single instance. An ES **lease lock** (a `locks` doc with an expiry, taken with `op_type=create` or a seq_no check) prevents two workers during deploys.
  - ES Serverless scales automatically.
- **Failure modes:**
  | Failure | What happens |
  |---|---|
  | Mistral down | Template briefings and alerts; reports fall back to a manual category |
  | A feed down | That layer is marked stale. Risk uses the other inputs, with the reason "rain data unavailable." |
  | Photon down | Telegram and Push are unaffected; the outbox retries |
  | Worker crash | Railway restarts it. The lease expires after 2 minutes, and the watermark resumes from ES. |
  | Railway region outage | Out of scope for the hackathon; revisit later |
- **Monitoring:**
  - The `logs-highground` data stream feeds a Kibana dashboard covering request rate and p95 latency, LLM calls and tokens, cache hit rate, feed age, outbox status, and alerts sent.
  - **Kibana alerting rules** notify the team's Telegram admin chat when any feed is older than 3× its interval, the error rate goes above 5%, there are dead letters, or the worker lease is missing.
  - `/status` is public.
- **Security:**
  - Separate ES API keys for web (read, plus writes to subscriptions, narratives and geocode-cache only) and for the worker (ingest writes).
  - Secrets live in Railway variables.
  - Rate limits: 60/min per IP, 5 subscriptions/day per handle, 10 reports/hour per IP.
  - Upload size limits, plus an **SSRF allowlist** for fetching `media_url` (Open311's Cloudinary domain only).
  - Phone numbers are encrypted with Fernet, with a hash used for lookup.

## Trade-offs (decision log)
| # | Decision | Chosen | Alternative | Why, and when to revisit |
|---|---|---|---|---|
| 1 | Datastore | **Elasticsearch for everything**, including subscriptions and the outbox | Postgres + Redis | One system, one backup story, and it shows Elastic. ES has no transactions; deterministic IDs, `op_type=create` and seq_no checks cover that. Revisit beyond 100k subscribers or for billing-grade consistency. |
| 2 | Risk timing | **Precompute per H3 cell** in the worker, with inline refresh | Compute on every request | Alerts need proactive evaluation anyway. Reads are cheap and consistent, and the replay uses the same code. Cost: up to 5 minutes of staleness, mitigated by the inline refresh. |
| 3 | Who decides risk | **Deterministic code** | The LLM | Trustworthy, testable, and calibratable. Mistral adds language, not judgment. |
| 4 | Agent runtime | **Chat completions with function calling** (stable) | Mistral Agents/Conversations API (beta) | Reliability. Agents plus MCP is an optional add-on. |
| 5 | Live source | **Open311 GET** for live data, DataSF for history | DataSF only | DataSF updates nightly; Open311 is real-time. Cost: Open311 lacks subtypes, so Mistral classifies them. |
| 6 | Integration style | **Polling with overlap windows** | Push or webhooks | The upstreams offer no push. Revisit if SF311 or NWS add streams. |
| 7 | Block unit | **H3 resolution 9**, with point-exact checks | Pure radius queries | Cacheable and easy to match for alerts. ES `geohex_grid` uses H3 too. |
| 8 | Map transport | **ES `_mvt` vector tiles** for points and cells; simplified GeoJSON for zones | All GeoJSON | Scales to 150k points on phones. |
| 9 | Process model | **Modular monolith** (web + worker, one image) | Microservices | Team size and time. Module boundaries (sources, risk, agent, alerts) are set up so pieces can be split out later. |
| 10 | Hosting | **Railway** | Cloud Run, Fly | The team already uses it. Single region. Revisit with official partners. |
| 11 | Channels | **iMessage + Telegram + Web Push** now; SMS later | SMS now | SMS needs 10DLC approval, which takes days to weeks. |
| 12 | Queue | **ES outbox** polled every 15 s | Redis or SQS | Fine up to about 10k alerts per storm. Revisit for larger fanout. |

## What to revisit as the system grows
- Move subscriptions and the outbox to Postgres and add a real queue once there are more than about 10k subscribers.
- Multi-region hosting.
- Turn on SMS after 10DLC approval.
- Turn on Open311 POST once the key arrives, and pursue a data partnership with SFPUC and DEM, including SFPUC rain gauges.
- A low-cost street flood-sensor pilot in the FloodNet style, at the top hotspots from section 1.
- Recalibrate the risk thresholds every season against new storms.
- ML anomaly detection once it's available on our ES project type.
- Multi-city configuration (Open311 and NWS exist nationwide).
- A formal privacy review, plus an accessibility review with disability-advocacy groups and community organizations (e.g., in the Mission and Bayview).

## Code style

**A functional core with typed seams, not full OOP.**
- **Pure functions for the logic,** with no I/O and no reading the real clock:
  - `risk.assess(cell, as_of, inputs)`
  - ES query builders in `tools.py`
  - feed parsers and normalizers
  - `verify.py`
  - alert hysteresis and matching rules
  - fallback templates
- **Pydantic models for data:** `Report`, `RiskState`, `Reason`, `Briefing`, `Subscription`, `OutboxItem`, `Inputs`. The same models produce Mistral's structured-output schemas and the FastAPI/OpenAPI docs.
- **`typing.Protocol` only where there are 2+ real implementations:**
  - `InputsProvider` (Live, Archive)
  - `Source` (8 feeds, each implementing just `fetch(since)`, with one shared `run_source()` for watermarks, backoff, circuit breaker and idempotent upserts)
  - `Channel` (Photon, Telegram, WebPush, later SMS)
- **Thin client wrappers** (`es.py`, `mistral_client.py`) hold connections, timeouts, concurrency limits and fallbacks.
- **Not used:** inheritance trees, manager/factory/repository layers, dependency-injection frameworks. Modules serve as the namespaces.
- **Rule:** add a class only for 2+ implementations or a held resource. Otherwise write a function.

## As built (changes found while implementing)

- **Live vs. history:** DataSF 311 (`vw6y-z8j6`) refreshes **nightly**. The live layer polls SF **Open311 GET** (`PUC:Water:Sewer`, `PW:BUF:Tree Maintenance`, `input:Pothole & Street Issues`), which is real-time. Both write to the same `_id = 311:<case_id>`. Because ingest pipelines don't run on update requests, the store reads the existing case, merges it in code (`hazards.merge_case`: DataSF subtype > Mistral > service code > keywords; the newest status wins), then re-indexes it through the enrich pipeline.
- **Time zones:** DataSF timestamps are floating Pacific time and Open311 timestamps are UTC. Everything is stored as UTC.
- **No flood depths:** `wppz-u2hi` holds water-surface elevations in SF City Datum, not depth, so the app shows the 100-yr zone and the storm + 24" SLR area only.
- **Geocoding:** done by Elasticsearch over DataSF EAS addresses (224k) and intersections (21k), using `search_as_you_type`, fuzzy matching and ordinal normalization ("03RD" = "3RD" = "THIRD"). The US Census geocoder is the fallback. Landmarks outside SF return "not found" rather than a wrong match.
- **One provider for live and replay:** the backtest needs about 1,137 cells × 96 steps, so inputs are materialized once into a `ReplayDataset` and read in memory by `ReplayInputs`. The live risk loop builds the same structure from ES each run (`riskloop.live_snapshot`), so live and replay run identical code. A full pass takes about 0.5 s.
- **Worker lease:** a single `lease` job renews the ES lock every 30 s, and jobs read the cached lease. SIGTERM releases it on deploy.
- **Calibration:** `replay calibrate` grid-searches thresholds on 6 storm days, maximizing F0.5, with Dec 31, 2022 held out. Result: High = 2.0 (see `data/replay/calibration.json`).
- **Archived weather fallback:** Open-Meteo's free tier is rate-limited per IP. `prepare` falls back to the SFO ASOS gauge (IEM) and records which source it used in each dataset's `sources`.
- **Apify actors** (schemas checked): `apify/website-content-crawler` and `apify/reddit-scraper` (official, pay per post). Several guessed start URLs returned 404 and were removed; only verified URLs are crawled.
