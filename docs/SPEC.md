# agent_instruction.md — Review Intelligence MVP

You are building an MVP SaaS that turns Play Store and App Store reviews into a prioritized, version-aware product-feedback report. Read this whole file before writing code. Build in the milestone order at the bottom. Do not add features that are not listed.

---

## 1. Product summary

A user submits an app (package/ID, stores, filters, sample size). The system:

1. Collects reviews, cleans and dedupes them, and snapshots the raw data.
2. Uses an LLM to **discover complaint categories** from a stratified sample.
3. Uses the LLM to **classify every review** against those categories (multi-label, with a relevance score and a note).
4. Uses the LLM to write an **executive summary and a prioritized action plan** per category.
5. Shows everything on a **dashboard** (charts, issues, proof reviews, version performance, unaddressed feedback).

A run is **immutable and reproducible**: re-running creates a new run; old runs never change.

---

## 2. Key design decisions (these deviate from the naive plan on purpose)

| # | Decision | Why |
|---|----------|-----|
| D1 | **Taxonomy sample is stratified random, not "first 200".** Stratify by store × star bucket × version. Default 10% of the cleaned set, clamped to **min 150, max 300** (if fewer than 150 cleaned reviews exist, use all of them as the sample). | Taking the first N biases toward one app version / one time window and yields a skewed taxonomy. |
| D2 | **Review type gate before categories.** Every review first gets `type` ∈ `complaint`, `praise`, `feature_request`, `question`, `spam_or_irrelevant`. Only `complaint` (and optionally `feature_request`) gets categorized. | Otherwise 5-star "love it!" reviews land in the null bucket and pollute it. |
| D3 | **Null bucket = vague complaints only** (`type=complaint` but no actionable detail, e.g. "worst app", "doesn't work"). Stored as category id `null`. Never mix with praise/spam. | Matches your intent; makes it a clean "ask for clarification" target list. |
| D4 | **Multi-label.** One review can map to several categories. Output CSV has one row per (review, category). | Real reviews mention several problems. |
| D5 | **Taxonomy can grow.** The classification pass may return `proposed_new_category` for a review that fits nothing. After all batches, a short **taxonomy reconciliation** step clusters these proposals (LLM call), adds accepted ones as new categories, and reassigns those reviews. Anything still unmatched → `other` (a real category, distinct from `null`). | First-chunk taxonomy will miss late-emerging issues. |
| D6 | **Relevance score is ordinal, not a probability.** Prompt asks for an integer 1–5 relevance + a separate 1–5 severity, stored as `relevance` and `severity`; `score = relevance/5` is exposed for sorting. Do not present it as "confidence %" in the UI; label it "relevance". | LLM self-reported confidences are poorly calibrated. |
| D7 | **Proof-review ranking** for the summary step = `0.5*relevance_norm + 0.2*severity_norm + 0.15*thumbs_up_norm + 0.15*recency_norm`, take top 50 per category (configurable). | Pure LLM score ignores social proof and freshness. |
| D8 | **Persistence: SQLite for state + CSV/JSON artifacts per run.** CSVs remain the human-facing exports; SQLite holds runs, status, config, and indexes for the dashboard. | CSV alone cannot track job state, history, or serve the dashboard efficiently. |
| D9 | **Reproducibility contract:** each run directory stores `config.json` (all inputs + code git hash + model name + prompt versions), raw snapshot, every intermediate artifact, and an LLM cache keyed by `sha256(model + prompt_version + rendered_prompt)`. LLM `temperature=0`. Re-run = new run; "replay" = re-run pipeline against a prior run's raw snapshot with cache hot. | Store data changes daily, LLMs are non-deterministic; snapshot + cache is the only honest way to reproduce. |
| D10 | **Language (India-aware):** default `language_filter="lenient"`: keep English **and Hinglish** (Hindi written in Latin script); drop only reviews written predominantly in non-latin scripts (e.g. Devanagari, Tamil) and count them in the run report. Do **not** rely on `langdetect` to accept/reject (it misclassifies Hinglish and short text); use a simple Unicode-script ratio check (≥ 70% Latin letters among alphabetic chars). Options: `"strict_en"` (use `langdetect`, keep `en` only) and `"off"`. Gemini handles Hinglish well, so the classification prompt must say reviews may be Hinglish and that notes must be written in English. | Many Indian-market reviews are Hinglish; a naive `en`-only filter would silently throw away a large share of real complaints. |
| D11 | **PII:** drop `userName`/user ids from LLM input and from exports shown in the dashboard; scrub emails/phones with regex before sending to LLM (include Indian mobile patterns, e.g. `+91`/10-digit numbers starting 6–9). | Privacy and cost. |
| D12 | **Single market for MVP: India.** `country` is fixed to `in` for both stores (Play `country="in"`, `lang="en"`; App Store `/in/` RSS). The API accepts `country` but only `"in"` is allowed in v1; anything else returns `INVALID_CONFIG`. Keep the field in `RunConfig` so multi-country can be added later without a schema change. | Simplifies collection and keeps results coherent; App Store cap becomes a plain ~500 for India. |
| D13 | **Default LLM provider: Google Gemini** via the official `google-genai` SDK, behind the provider abstraction so others can be added. | User decision. |

---

## 3. Known data-source constraints (design around these)

**Play Store (`google-play-scraper`)**
- `reviews(app_id, lang, country, sort, count, filter_score_with, continuation_token)` is paginated; loop with `continuation_token` until `count` reached or exhausted. Use `Sort.NEWEST` for date-window filtering (stop paging once `at` < `date_from`).
- Useful fields: `reviewId, content, score, thumbsUpCount, reviewCreatedVersion, at, replyContent, repliedAt, appVersion`.
- `reviewCreatedVersion` can be null; treat as `unknown` version.
- Unofficial scraper: add retry with exponential backoff, a small sleep between pages, and handle `google_play_scraper.exceptions.NotFoundError` → "app not found".
- App metadata via `google_play_scraper.app(app_id)` (name, icon, current version, rating, ratings count). Use it for validation.

**App Store (public RSS feed)**
- Endpoint pattern: `https://itunes.apple.com/{country}/rss/customerreviews/page={1..10}/id={app_id}/sortby=mostrecent/json`.
- **Hard cap: ~10 pages × 50 = ~500 reviews for the India storefront** (`country=in`, see D12). The MVP is single-country, so App Store can never satisfy a `sample_size` above ~500. When both stores are selected, collect up to `sample_size` from Play and up to ~500 from App Store, and report actual per-store counts. Surface this limit in the API response and UI ("App Store capped at ~500 reviews").
- Fields: id, author (drop), rating, title, content, version (`im:version`), updated date. **The RSS feed does not include developer replies**; set `dev_reply` empty and `has_dev_reply=null` (unknown) for iOS. Show "N/A for App Store" in the UI, do not claim "unaddressed" for iOS reviews.
- Validate app with `https://itunes.apple.com/lookup?id={app_id}&country={cc}`; empty `results` → app not found. Accept an app name too: resolve via `https://itunes.apple.com/search?term=...&entity=software&limit=5` and return candidates for the user to pick.
- First page can be empty for apps with few reviews → "not enough reviews", not a crash.

---

## 4. Tech stack

- Python 3.11+, `uv` or `pip` + `requirements.txt` (pin versions).
- **FastAPI** + Uvicorn for API and for serving the dashboard.
- **SQLite** via `sqlalchemy` (or `sqlite3` + small repo layer).
- **pandas** for CSV work. **pydantic v2** for all schemas, including LLM output validation.
- **LLM provider abstraction** (`llm/client.py`, interface: `generate_structured(prompt, schema, *, temperature=0) -> pydantic model`). Default implementation: **Gemini** via the official `google-genai` SDK (`from google import genai`), using structured output (`response_mime_type="application/json"` with `response_schema` set to the pydantic model) and `temperature=0`. Model name comes from env `LLM_MODEL`; **do not hardcode it**. Default to a current Flash-tier Gemini model (cheap, fast, large context; fine for batch classification) and use a stronger model for summarization only if `LLM_SUMMARY_MODEL` is set. Before choosing defaults, check Google's current model list/docs, since model names change often. Always validate the response with pydantic and retry up to 3× on invalid JSON (feed back the validation error). Gemini-specific handling: map HTTP 429/quota to retry with exponential backoff + jitter (respect any retry-after hint); map safety-blocked / empty candidates to `LLM_BLOCKED` for that batch (retry once with the batch split in half, then mark the affected reviews `classification_failed`); keep concurrency low by default (`MAX_LLM_CONCURRENCY=3`) because free-tier rate limits are tight.
- **Dashboard:** single static page (Alpine.js or vanilla JS) + Chart.js, served from FastAPI `/dashboard`. No Node build step in MVP.
- **Job execution:** `POST /runs` creates a run row and launches the pipeline in a background worker (a single-process `ThreadPoolExecutor(max_workers=1)` or `subprocess`), which executes the stage scripts in order. Status polled via `GET /runs/{id}`.
- Tests: `pytest`; mock store HTTP and the LLM client.

Env vars (`.env.example`): `LLM_PROVIDER=gemini`, `GEMINI_API_KEY`, `LLM_MODEL`, `LLM_SUMMARY_MODEL` (optional), `DATA_DIR=/app/data` (in Docker; `./data` locally), `MAX_LLM_CONCURRENCY=3`, `LOG_LEVEL=INFO`, `PORT=8000`. The app must fail fast at startup with a clear message if `GEMINI_API_KEY` is missing (the `/health` endpoint should still report `llm_configured: false` rather than crash, so the dashboard can show a setup banner).

---

## 5. Repository layout

```
review-intel/
  agent_instruction.md
  README.md
  .env.example
  Dockerfile
  docker-compose.yml
  .dockerignore
  pyproject.toml / requirements.txt
  app/
    main.py                  # FastAPI app, mounts routers + dashboard
    api/
      apps.py                # POST /apps/validate
      runs.py                # run lifecycle endpoints
      reports.py             # report data endpoints
    core/
      config.py              # settings
      db.py, models.py       # SQLite
      errors.py              # typed errors + error codes
      runner.py              # orchestrates pipeline stages, status updates
      logging.py
    pipeline/                # each stage is runnable standalone: python -m app.pipeline.s01_collect --run-id X
      s01_collect.py
      s02_clean.py
      s03_sample_taxonomy.py
      s04_classify.py
      s05_reconcile.py
      s06_summarize.py
      s07_aggregate.py       # version stats, chart-ready JSON
    sources/
      playstore.py
      appstore.py
      base.py                # ReviewSource protocol
    llm/
      client.py              # provider interface + cache + retry
      gemini.py              # default provider (google-genai)
      fake.py                # deterministic fake for tests
      prompts/               # versioned prompt files: taxonomy_v1.md, classify_v1.md, ...
      schemas.py             # pydantic output models
    utils/
      text.py                # normalize, PII scrub, lang detect
      versions.py            # tolerant version parsing/ordering
  dashboard/
    index.html, app.js, styles.css
  data/
    app.db
    runs/<run_id>/           # see section 7
  tests/
```

Every stage reads inputs from and writes outputs to `data/runs/<run_id>/`, updates its row in `run_stages`, and is **idempotent** (re-running a stage overwrites only its own outputs). `runner.py` just calls them in order and stops on the first failure with a typed error.

---

## 6. API spec

All responses JSON. Errors use `{ "error": { "code": "...", "message": "...", "details": {...} } }` with appropriate HTTP status.

### `POST /apps/validate`
Request: `{ "stores": ["playstore","appstore"], "play_package": "com.x.y", "appstore_id": "123456789" | null, "app_name": "optional", "country": "in" }` (only `"in"` is accepted in v1, see D12)
Behavior: calls store metadata endpoints. Returns per-store `{found, name, icon, current_version, rating, ratings_count}` and, if an app name was given for iOS, candidate matches. Used by the form before a run is created.

### `POST /runs`
Request body (pydantic `RunConfig`):
```json
{
  "app_name": "string",
  "stores": ["playstore", "appstore"],
  "play_package": "com.x.y",
  "appstore_id": "123456789",
  "country": "in",
  "language_filter": "lenient",
  "filters": {
    "stars": [1,2,3],
    "date_from": "2026-01-01",
    "date_to": "2026-10-27",
    "versions_include": null,
    "versions_exclude": null
  },
  "sample_size": 1000,
  "taxonomy_sample_pct": 0.10,
  "taxonomy_sample_min": 150,
  "taxonomy_sample_max": 300,
  "top_k_proof_reviews": 50,
  "version_context": {
    "baseline_versions": null,
    "treat_dev_replied_as_addressed": true
  }
}
```
Rules: `sample_size` ≥ 50 (soft cap 5000 for MVP); `taxonomy_sample_pct` in (0, 0.5]; `stars` default 1–5; at least one store required, and each selected store needs its identifier. Return `201 { "run_id": "...", "status": "queued" }`. Validate synchronously (app exists) before queueing; fail fast with `APP_NOT_FOUND`.

### `GET /runs`  — list runs (id, app, created_at, status, counts)
### `GET /runs/{run_id}` — status, per-stage status/timings, warnings, error (if any), config
### `POST /runs/{run_id}/rerun` — new run with the same config (fresh scrape). Body option `{ "reuse_snapshot": true }` replays on the old raw snapshot with the LLM cache (reproducibility check).
### `POST /runs/{run_id}/cancel` — cooperative cancel checked between batches.

### Report endpoints (all read from run artifacts/SQLite)
- `GET /runs/{id}/report` → full dashboard payload (see section 9)
- `GET /runs/{id}/categories` → list with counts, avg severity, trend
- `GET /runs/{id}/categories/{cat_id}/reviews?limit=&offset=&sort=` → proof reviews
- `GET /runs/{id}/null-reviews?unaddressed_only=true` → vague complaints (default: no dev reply)
- `GET /runs/{id}/versions` → per-version metrics
- `GET /runs/{id}/export/{artifact}` → download CSV/JSON (`reviews_clean`, `classifications`, `taxonomy`, `summary`)

Dashboard "Run analysis" button → `POST /runs` using the form config (or `/rerun` from an existing report), then polls `GET /runs/{id}` every 2 s showing stage progress.

---

## 7. Pipeline stages and data contracts

Run directory: `data/runs/<run_id>/`
```
config.json            # inputs + git hash + model + prompt versions
raw_reviews.csv        # exactly what was scraped (snapshot, never modified)
reviews_clean.csv
taxonomy.json
classifications.csv
taxonomy_final.json
summary.json
aggregates.json
llm_cache/             # one JSON per cached call
logs/pipeline.log
```

### s01_collect
- For each store/country, fetch up to `sample_size` reviews matching filters (stars/date applied at source where supported, otherwise post-filter). Over-fetch ~1.3× when filtering on languages/duplicates will shrink the set, but stop at hard limits.
- Normalize to the unified schema and write `raw_reviews.csv`:
  `review_id, store, country, rating, title, content, thumbs_up, app_version, created_at, dev_reply, dev_replied_at, has_dev_reply, fetched_at`
- `review_id` = `{store}:{native_id}`.
- Errors: `APP_NOT_FOUND`, `SOURCE_UNAVAILABLE` (after retries), `NO_REVIEWS_FOUND`. Partial success is allowed when one store fails and another succeeds: record a warning, continue, and mark the report as partial.

### s02_clean
Order: drop null/empty content → strip/normalize whitespace and unicode (NFKC) → strip emoji-only/very short (<3 words after cleaning unless rating ≤2 and has a clear keyword) reviews, **but count them** → scrub PII → language filter → **dedupe** (exact on normalized text; then near-duplicate with `rapidfuzz` ratio ≥ 95 within the same rating; keep the one with higher thumbs_up/most recent) → parse versions → add `clean_text`, `word_count`, `lang`, `version_norm`.
Write `reviews_clean.csv` and `aggregates` counters (`dropped_empty`, `dropped_short`, `dropped_lang`, `dropped_dupes`).
Gate: if `len(clean) < 30` → fail with `NOT_ENOUGH_REVIEWS` (include counts and a hint to loosen filters). If `30 ≤ len < 100` → continue with a warning `LOW_SAMPLE`.

### s03_sample_taxonomy
- Draw the taxonomy sample per D1 with a fixed `random_seed` derived from `run_id` (store the seed in config).
- Call the LLM (single call if ≤ 300 reviews) with `taxonomy_v1` prompt: classify each review's `type`, and for complaints design 5–15 mutually distinguishable categories.
- Output `taxonomy.json` (validated by pydantic):
```json
{
  "categories": [
    {"id": "c01", "name": "Login failures", "definition": "...", "include_examples": ["review_id", "..."], "exclude_hint": "..."}
  ],
  "null_category": {"id": "null", "definition": "Complaint with no actionable detail"},
  "sample_review_types": {"<review_id>": "complaint"}
}
```
- Category ids are stable slugs/ids assigned in code, not by the LLM.

### s04_classify
- Process **all** cleaned reviews (including the taxonomy sample; keep it simple and consistent, so everything gets classified by the same prompt) in batches of **25** (configurable), concurrency ≤ `MAX_LLM_CONCURRENCY`.
- Input per review: `review_id, rating, clean_text, app_version`. Prompt includes the taxonomy (id, name, definition) and rules.
- Output per review (pydantic): `type`, and for complaints a list of `assignments: [{category_id, relevance 1-5, severity 1-5, note (≤200 chars)}]`, or `category_id="null"` if vague, or `proposed_new_category: {name, definition}` if nothing fits.
- Write `classifications.csv`, one row per assignment:
  `review_id, type, category_id, relevance, severity, score, note, proposed_new_category, batch_id`
  Reviews of type praise/question/spam get one row with an empty `category_id`.
- Validation: every `review_id` in a batch must appear in the response exactly once; missing ids are re-queued individually (max 2 retries), then recorded as `classification_failed` and counted in warnings. Never silently drop.
- Checkpoint after each batch so a crash/cancel can resume from the last completed batch.

### s05_reconcile
- Collect all `proposed_new_category` rows. If < 5 unique-ish proposals or < 2% of complaints, skip and map them to `other`. Otherwise one LLM call clusters the proposals into new categories (or `other`), then **re-classify only the affected reviews** against the expanded taxonomy.
- Write `taxonomy_final.json` and update `classifications.csv` (append a `taxonomy_version` column).
- Merge or flag categories with < 2 reviews as `minor` (still listed, not summarized).

### s06_summarize
- For each category with ≥ 3 reviews (excluding `null`): pick top-K proof reviews per D7. Call the LLM with category definition + proof reviews + counts + version spread. Output (pydantic):
  `headline, summary, root_causes[], affected_versions[], user_quotes[] (verbatim review_ids only, never fabricated text), recommended_actions[{action, rationale, effort: S|M|L, impact: 1-5}]`
- One final LLM call produces the **executive summary** and a **priority action plan** across categories. Priority score is computed **in code** (not by the LLM):
  `priority = volume_share * 0.35 + avg_severity_norm * 0.25 + rating_drag * 0.2 + recency_weight * 0.1 + trend_up * 0.1`
  The LLM only receives the ranked list and writes narrative around it, and must not reorder it. Include the formula in the report's "How to read this" tooltip.
- Quotes must be validated: every cited `review_id` exists in the run. Reject and retry otherwise.
- Write `summary.json`.

### s07_aggregate (no LLM)
Produces `aggregates.json` for charts:
- Rating distribution (by store).
- Review volume over time (weekly).
- Category share overall and by store.
- **Per-version table**: `version, n_reviews, avg_rating, pct_complaints, top categories, dev_reply_rate (Play only)`; order versions semantically.
- **Category × version heatmap** (share of that version's complaints).
- **Version resolution signals** per category: for each category, compare share in the latest N versions against earlier versions. Label `likely_resolved` (share dropped ≥ 50% and ≥ 2 later versions with ≥ 20 reviews each), `regression` (appears or grows ≥ 50% in newer version), `persistent`, or `insufficient_data`. Also surface dev replies containing "fixed/resolved/update/latest version" as `dev_claims_fixed` evidence. Mark these as **heuristic**, not facts.
- **Unaddressed feedback**: complaints with `has_dev_reply == false` (Play only), the null category list, sorted by thumbs_up desc then recency.

---

## 8. Error handling

Define typed exceptions in `core/errors.py`, each with `code`, `http_status`, `user_message`, `retryable`.

| Code | When | Behavior |
|------|------|----------|
| `APP_NOT_FOUND` | Store lookup empty/404 | 404 at `/apps/validate` and `/runs`; clear message naming the store and id tried; if iOS name search, return candidates |
| `INVALID_CONFIG` | Bad filters/ranges/pct | 422 with field-level errors |
| `NO_REVIEWS_FOUND` | Zero reviews after fetch | Run `failed`, message suggests widening filters |
| `NOT_ENOUGH_REVIEWS` | < 30 after cleaning | Run `failed`, show counts at each cleaning step |
| `LOW_SAMPLE` | 30–99 after cleaning | Warning only; banner on dashboard "results are directional" |
| `STORE_LIMIT_REACHED` | App Store cap hit before `sample_size` | Warning; report shows actual vs requested count |
| `SOURCE_UNAVAILABLE` | Network/rate-limit after 4 retries (backoff 1,2,4,8s + jitter) | If other store succeeded: partial run with warning; else fail |
| `LLM_INVALID_OUTPUT` | Schema validation fails 3× | Fail that batch; mark reviews `classification_failed`; if > 10% failed, fail the run |
| `LLM_UNAVAILABLE` | Missing/invalid `GEMINI_API_KEY`, quota exhausted, 429 after retries, 5xx | Missing/invalid key: fail immediately with a setup message (not retryable). 429/5xx: retry with backoff, then mark run `failed` with a resumable checkpoint |
| `LLM_BLOCKED` | Gemini returns a safety block or empty candidates for a batch | Split batch and retry once; remaining reviews marked `classification_failed` (counts toward the 10% failure threshold) |
| `RUN_CANCELLED` | User cancel | Stop between batches, status `cancelled` |

Rules: never return stack traces to clients (log them); every failure writes `error.json` in the run dir and populates `runs.error_code/error_message`; the dashboard shows the message and a "Retry / Edit filters" action; stages must be resumable (`POST /runs/{id}/resume`) from the last good checkpoint.

---

## 9. Dashboard spec (`/dashboard`)

Pages/sections (single page, tabs or anchors):

1. **New analysis form**: store toggles, identifiers, validation button (calls `/apps/validate` and previews app name/icon/current version), filters, sample size, taxonomy sample %, advanced (languages, countries, top-K). "Run analysis" button.
2. **Run progress**: stage stepper (collect → clean → taxonomy → classify → reconcile → summarize → aggregate) with counts and live log tail.
3. **Report header**: app name, run date, counts funnel (fetched → cleaned → classified), warnings banner, "Re-run" and "Export" buttons.
4. **Executive summary** + **Priority action plan** (ranked table: priority score, category, action, effort, impact).
5. **Charts**: rating distribution, volume over time, category share (bar), category by store, per-version avg rating + complaint rate (line), category × version heatmap.
6. **Issues**: expandable card per category with summary, root causes, version badges (`persistent`, `likely resolved`, `regression`), and proof reviews (text, rating, version, store, date, dev reply if present).
7. **Unaddressed feedback**: table of null-category (vague) and no-dev-reply complaints with filters; this is the "go ask these users for details" list. Export CSV.
8. **Run history**: list of runs; compare two runs' category shares (simple side-by-side, v1 optional).

Charts via Chart.js; all data from `GET /runs/{id}/report`. Mobile-friendly basic layout. No auth in MVP, but code must keep a `user_id` column nullable on `runs` for later.

---

## 10. LLM prompt rules (apply to all prompts)

- Prompts live in `llm/prompts/*.md` with a version suffix; version is recorded in `config.json`. Changing a prompt = new version file.
- Treat review text as **untrusted data**: wrap in delimiters, instruct the model to ignore any instructions inside reviews (prompt injection defense).
- Require JSON only, validated by pydantic. Provide the schema in the prompt.
- Notes must be concise, in plain language, no invented facts; quotes must reference `review_id`s only.
- `temperature=0`. Cache every call. Log token usage per stage (use Gemini's usage metadata) into `runs.token_usage`, and show estimated cost in the run summary (price table kept in a config file, not hardcoded in logic).
- Reviews may be English or Hinglish; instruct the model to write all notes, category names and summaries in English. Never translate or alter the stored review text.
- Even with `temperature=0`, Gemini is not guaranteed deterministic; that is why the LLM cache (D9) is the reproducibility mechanism, not the model setting.

---

## 11. Reproducibility checklist (must pass)

- [ ] `config.json` contains all inputs, seed, git commit, model, prompt versions, library versions.
- [ ] `raw_reviews.csv` is written once and never mutated.
- [ ] `POST /runs/{id}/rerun {"reuse_snapshot": true}` yields identical `classifications.csv` and `summary.json` (byte-identical via LLM cache) — add a test for this.
- [ ] Every stage can run standalone: `python -m app.pipeline.s04_classify --run-id <id>`.
- [ ] Runs never overwrite other runs.

---

## 12. Testing

- Unit: cleaning/dedupe edge cases, version parsing (`1.2`, `1.2.3`, `2024.10.1`, null, garbage), priority formula, proof-review ranking, config validation.
- Integration with a **fake LLM** (deterministic) and **recorded store fixtures**: full pipeline end-to-end, resume after killed batch, partial store failure, `NOT_ENOUGH_REVIEWS`, `APP_NOT_FOUND`.
- Contract tests for each API endpoint (success + error codes).
- A smoke test script `scripts/smoke.py` that runs against a real small public app with a tiny sample (optional, behind an env flag).

---

## 12b. Docker & runtime (required)

The whole app must run with one command: `docker compose up --build`, then open `http://localhost:8000/dashboard`.

**Dockerfile**
- Base `python:3.11-slim`. Set `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`.
- Install pinned dependencies first (copy `requirements.txt`, `pip install --no-cache-dir -r requirements.txt`) for layer caching, then copy `app/` and `dashboard/`.
- Create and run as a **non-root user**; `/app/data` must be writable by that user.
- Install `git` only if needed for recording the commit hash; otherwise pass the hash as a build arg (`ARG GIT_SHA`, exposed as env `GIT_SHA`) and have `config.json` read it from env, falling back to `unknown`.
- `EXPOSE 8000`; `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`.
- `HEALTHCHECK` hitting `/health` using Python's stdlib (`urllib`), so no `curl` install is needed.

**docker-compose.yml**
- One service `web`, `build: .`, `ports: ["8000:8000"]`, `env_file: .env`, `environment: { DATA_DIR: /app/data }`.
- Bind mount or named volume for `/app/data` so runs, SQLite DB and the LLM cache survive container restarts. Document the choice in the README (named volume `review_intel_data` by default, with a commented bind-mount alternative `./data:/app/data`).
- `restart: unless-stopped`.

**Runtime rules**
- Run **a single Uvicorn worker** (SQLite + in-process job runner; do not use `--workers > 1`). Document why in the README.
- On startup, mark any runs left in `running` state (from a killed container) as `interrupted` so they can be resumed via `POST /runs/{id}/resume`; never leave them stuck.
- Handle `SIGTERM` gracefully: set the cancel flag, let the current batch finish (or checkpoint), then exit.
- Logs go to stdout (JSON or plain) in addition to the per-run `logs/pipeline.log`, so `docker compose logs` is useful.
- No secrets in the image or repo: `.env` is git-ignored and docker-ignored; ship `.env.example` only. `.dockerignore` excludes `.git`, `data/`, `__pycache__`, `.env`, `tests/` caches.
- The container must work without network access to anything except Google Play, `itunes.apple.com`, and the Gemini API. Document this for firewalled environments.
- Add a `make`/script shortcut section in the README: `docker compose up --build`, `docker compose run --rm web pytest`, `docker compose run --rm web python -m app.pipeline.s04_classify --run-id <id>` (pipeline stages must be runnable inside the container).

---

## 13. Milestones (build in this order, commit after each)

0. **M0 — Docker skeleton:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, and a minimal FastAPI app with `/health`. Verify `docker compose up --build` works before writing any other code, and keep it working at every later milestone.
1. **M1 — Skeleton:** repo, config, SQLite models, error types, run lifecycle endpoints (stubbed pipeline).
2. **M2 — Collection:** `sources/playstore.py`, `sources/appstore.py`, `/apps/validate`, `s01_collect` with fixtures and error cases.
3. **M3 — Cleaning:** `s02_clean` with counters and gates.
4. **M4 — LLM layer:** provider interface, Gemini implementation (structured output, 429 backoff, safety-block handling), cache, retry, pydantic schemas, fake LLM for tests.
5. **M5 — Taxonomy + classification:** `s03`, `s04`, `s05` with checkpoint/resume.
6. **M6 — Summaries + aggregates:** `s06`, `s07`, priority formula, version signals.
7. **M7 — Runner + job control:** orchestrated run, status polling, cancel, resume, rerun with snapshot reuse.
8. **M8 — Dashboard:** all sections from section 9.
9. **M9 — Hardening:** reproducibility test, README with setup and architecture diagram, `.env.example`, sample run docs.

## 14. Definition of done (MVP)

- From a clean checkout with a `.env` containing `GEMINI_API_KEY`, `docker compose up --build` serves the dashboard at `http://localhost:8000/dashboard`, data persists across `docker compose down && up`, and the container healthcheck passes; a user enters an app, clicks **Run analysis**, sees progress, and gets a complete report with charts, issues, proof reviews, per-version performance, and the unaddressed-feedback list.
- All errors in section 8 are reproducible in tests and show human-readable messages.
- Reproducibility checklist in section 11 passes.
- README documents setup, API, pipeline, limitations (App Store ~500-review cap for India, no iOS dev replies, unofficial Play scraper, heuristic version signals, single-worker design, Gemini rate limits on free tier, India-only in v1).

## 15. Explicit non-goals for MVP

Auth/billing/multi-tenancy, multi-country sampling, other LLM providers (interface exists, only Gemini implemented), scheduled runs, reply-to-review automation, competitor comparison, full non-English analysis (Hinglish is supported; Devanagari/other scripts are filtered out), embeddings/vector search (note as a v2 option for better proof-review ranking), a JS build pipeline.
