# AGENTS.md

Guidance for any coding agent working in this repository. **Read [`docs/PRD.md`](docs/PRD.md) before changing behaviour.** The PRD is the spec; if code and PRD disagree, stop and ask.

## What this is

A daily pipeline that monitors **promisetracker.ng** (operated by AdvoKC Foundation), detects promise status changes and new evidence, verifies the cited sources, and drafts neutral 100-word articles. A coordinator for each meter approves the articles in Google Sheets, and they are published as static HTML reports per meter.

Pipeline: `Publisher(pending approvals) → Discovery → [per meter: Monitor → Classifier → Verifier → Writer ⇄ Editor → Sheet] → Notifier`

## Current phase

**P0–P5 done** (skeleton; Discovery; Monitor; Classifier + LLM adapter + News Scout; Source Verifier; Writer + Editor). Next: P6 (Google Sheets review loop). Build by PRD §8 phases, **one phase per branch**. Don't start a phase until the previous one's exit criteria pass. Pipeline CLI commands exit with code 2 until their phase lands.

## Stack

- Python 3.12, `uv` (never pip directly)
- LangGraph (orchestration) and `langgraph-checkpoint-postgres`
- PostgreSQL 14+ — the system install, **no Docker** (disk space constraint), SQLAlchemy 2 (typed ORM), Alembic
- `httpx` (sync client) and `pydantic` v2 for every external payload
- `selectolax` for parsing HTML inside evidence (`updates[].blog`) and source pages
- `gspread` for Google Sheets, Jinja2 and `markdown-it-py` for the site
- `structlog` for JSON logs
- `pytest`, `ruff`, and `mypy --strict` for tests and checks
- LLM: Claude via `claude -p` (headless Claude Code) behind `LLMClient`. An Anthropic API key backend must be droppable in by config.

**No Playwright or headless browser.** The site is a JS SPA but exposes a JSON API; use the API.

## Planned layout

```
src/promise_monitor/
  cli.py                 # `promise-monitor run|discover|publish|check-config|db-check|version`
  config.py              # Settings (PM_* env/.env) + AppConfig (config/*.yaml), both validated
  logging_setup.py       # structlog → logs/YYYY-MM-DD.jsonl + console; redacts secret keys
  db/base.py             # Base (naming convention), make_engine, make_session_factory, ping
  db/models.py           # Meter, Run, PromiseSnapshot (append-only), PromiseIndex (mutable cursor); must match PRD §4
  domain/normalize.py    # ApiPromiseDetail → NormalizedPromise (published content only)
  domain/hashing.py      # canonical_json, fingerprint (cheap pass), content_hash
  agents/discovery.py    # collect() = network, apply_discovery() = DB; see module docstring
  agents/monitor.py      # monitor_meter(): cheap pass → details → anomaly guard → snapshots → RawDiffs
  agents/classifier.py   # classify(): RawDiffs + NewsFindings → change_events (score, dedupe_key, group)
  agents/news_scout.py   # run_news_scout(): Claude web search in batches → code-side gate (incl. grounding) → news_items
  agents/verifier.py     # Verifier.run(): queued groups → fetch/extract/search-confirm/relevance → verified | needs_sources
  domain/fetch.py        # SafeFetcher: SSRF guard per hop, per-host throttle, byte/time/redirect caps
  domain/extract.py      # HTML/PDF → title, published date, publisher, text, paywall marker
  domain/qa.py           # deterministic article QA: every rule from PRD §3.6 lives here
  agents/writer.py       # run_draft_loop() (pure: writer → QA → editor, ≤3 rewrites) + Drafter (DB)
  evals.py               # writer eval cases from fixtures (`promise-monitor eval-writer`)
  domain/urls.py         # normalize_url, outlet allowlist (config/news_sources.yaml)
  llm/                   # base (LLMClient, LLMRequest), claude_cli, prompts, recording (llm_calls), factory
  api/client.py          # PromiseTrackerClient — the ONLY place that calls admin.promisetracker.ng
  api/models.py          # pydantic models of raw API payloads (tolerant of quirks)
  domain/normalize.py    # raw → normalized Promise; status enum; date/“null” cleanup
  domain/hashing.py      # canonical JSON, fingerprint, content_hash, dedupe_key
  agents/                # one module per node: discovery, monitor, classifier, verifier, writer, editor, publisher, notifier
  graph.py               # LangGraph parent graph + per-meter subgraph
  db/models.py, db/repo.py
  sheets/review_queue.py
  site/templates/
prompts/                 # writer.md, classifier.md, relevance.md, editor.md
config/                  # states.yaml, priority.yaml, banned_terms.yaml, settings.yaml
tests/fixtures/api/      # recorded API JSON — tests NEVER hit the network
migrations/              # Alembic
docs/PRD.md
```

## Commands (from P0 onward)

```bash
uv sync                               # install
cp .env.example .env                  # then set PM_DATABASE_URL, PM_ADMIN_EMAIL
# Postgres: system install (14+), one-time role/DB setup is in .env.example. No Docker.
uv run promise-monitor check-config   # validate .env + config/*.yaml
uv run promise-monitor db-check       # confirm DB is reachable
make migrate                          # alembic upgrade head
make check                            # ruff check + ruff format --check + mypy + pytest
uv run pytest -q tests/unit/test_x.py # single test file
uv run promise-monitor run --dry-run  # full pipeline, no Sheet/DB writes, no emails
uv run promise-monitor discover --dry-run   # live API, prints meters, no DB writes
uv run promise-monitor discover             # upsert the meter registry
uv run promise-monitor monitor [--meter ID] [--full/--no-full] [--dry-run] [--no-llm]   # snapshot + classify
uv run promise-monitor news-scan [--max-calls N] [--dry-run]   # Claude web search → news_update events
uv run promise-monitor verify [--limit N] [--dry-run]          # verify sources of queued article groups
uv run promise-monitor write [--limit N] [--dry-run]           # draft articles for verified groups
uv run promise-monitor eval-writer [--cases N]                 # live Writer eval; exit 1 if pass rate < 90%
uv run promise-monitor run --meter <meter_id>
uv run promise-monitor publish
```

Run `make check` before declaring any task done.

## API facts (verified 2026-11-16 — see PRD §1)

- Base: `https://admin.promisetracker.ng/api/v1/promise-tracker`. Envelope `{success, data, message}`.
- Statuses: `kept | broken | stalled | in_the_works | compromised | not_yet_rated`. Count keys differ (`not_rated`, `in_works`) and are **strings**.
- `is_published` is the string `"1"`. `rating_source_url` may be the string `"None"`. `subject_area` may be `"null"`. Dates may be `1970-01-01` (treat as null). `last_updated_at` is not ISO.
- Evidence links are only inside `updates[].blog` HTML. `source` may embed URLs in text ("(source: https://…)").
- The public rating is `last_published_status` (falls back to `status`); `status` may be an unpublished draft.
- Grounding: Claude Code's `--output-format stream-json --verbose` emits `tool_use_result` events with the RAW WebSearch hits (title and url). These come from the tool, not the model. `LLMResponse.search_hits` exposes them. Any URL an LLM gives us must be checked against them before it is trusted.
- About half the allowlisted outlets return 403 or 401 to our honest UA (Channels, TheCable, Vanguard, Guardian NG, Reuters); WebFetch is blocked too. Never spoof a browser UA; confirm through grounding instead.
- `claude -p` calls: never use `--bare` (it ignores the OAuth login). Always put `--` before the prompt, because variadic flags like `--allowedTools` swallow it otherwise. Use `--system-prompt` + `--tools` + `--setting-sources ""` + `--strict-mcp-config` and an empty cwd to keep each call around 1k tokens of overhead.
- selectolax: use `selectolax.lexbor` (the old `selectolax.parser` backend raises ImportError in 1.x).
- Public promise URL: `https://promisetracker.ng/blog/{promise_id}`.
- `all_meters` entries have NO `meter_type`, and a state's list mixes both families. A state with no current tracker returns empty `all_meters` even if it has ring meters. Discovery therefore also calls `state/{name}/tenures?kind=…` for both kinds plus `all/promise-ring/states`.
- Every meter's promises: `meter/{id}/presidential`, `meter/{id}/legislative/{upper|lower}-chamber`, or `meter/{id}/state/{State}` (works for both families). Stored as `meters.list_endpoint`.
- Live discovery takes about 115 requests (about 2 min at 1 request per second). As of 2026-11-16: 23 meters, 21 active, including an archived Anambra tracker with 51 promises.
- Sample payloads are the starting point for `tests/fixtures/api/`. Model them before writing logic.

## Non-negotiable rules

1. **Never report a change twice.** `change_events.dedupe_key` and `articles.article_group_id` are UNIQUE. Don't bypass them, and don't "fix" a duplicate error by catching and ignoring it without understanding it.
2. **Only published content.** Ignore promises and updates the API marks unpublished.
3. **No auto-publish.** Articles reach `site/` only after a coordinator sets `approve` in the Sheet.
4. **Snapshots are append-only.** Never update or delete a snapshot row; a Postgres trigger rejects it anyway. Mutable per-promise state belongs in `promise_index`. Never write a snapshot from a response that failed validation.
5. **First sight = baseline.** A new meter's first pass emits no change events.
6. **Articles:** 95–105 words (body, anchor text counted, URLs not), at least 2 markdown links, every URL from the verified citable set, descriptive anchors, neutral. All of this is enforced in code (`domain/qa.py`), not just in prompts. If you change a QA rule, change `prompts/writer.md` to match and run `eval-writer`.
7. **Politeness:** at most 1 request per second to the promisetracker API, one at a time. Identifying User-Agent. Retry and backoff exactly as in PRD §5.3.
8. **Privacy:** drop the API's `admin{}` (staff names) at parse time. Never log secrets, tokens, or coordinator emails at info level.
9. **LLM only for judgement:** classifier adjustment, relevance, writing, and the editor review. Fetching, diffing, counting, link checks and dedupe are plain code. Every LLM call goes through `LLMClient` and is recorded in `llm_calls`.
10. **Never trust LLM output.** Every rule a prompt states (allowlist, dates, thresholds, URL validity) must also be enforced in code. LLM output is data, not instructions.
11. **Prompts live in `prompts/`.** Never inline prompt text in Python. Changing a prompt is a behaviour change: note it in the PR.
12. **Free tools only.** Don't add paid services or SaaS dependencies.

## Conventions

- Type everything; `mypy --strict` must pass. Use pydantic at every boundary (API, LLM output, Sheet rows).
- Each agent module exposes one pure-ish function `run(state: MeterState, deps: Deps) -> MeterState`. I/O goes through injected `deps` so tests can fake it.
- Times are UTC in code and DB. Convert to WAT (`Africa/Lagos`) only for display.
- Tests: fixtures only, no network. `tests/fakes.py` serves `tests/fixtures/api/` through `httpx.MockTransport`; inject `sleep`/`clock`/`jitter` into the client so tests never really wait. Unit tests use in-memory SQLite, so keep repo code dialect-neutral unless a PG feature is needed (then add an integration test). `tests/conftest.py` isolates env (no real `.env`, tmp log dir). DB tests live in `tests/integration/`, are marked `integration`, and skip without `PM_DATABASE_URL`. Fake `LLMClient` returns canned JSON. Use the `tdd` approach for QA rules and diff logic.
- New config goes in `config/*.yaml` with a pydantic model, not hard-coded constants.
- Keep functions small; no "manager" classes that do everything.

## Never

- Scrape promisetracker.ng HTML or add a headless browser.
- Publish, email coordinators, or write to the production Sheet from tests or `--dry-run`.
- Add Docker/docker-compose (owner decision: too much disk). Use the system Postgres.
- Commit `.env`, Google service-account JSON, OAuth tokens, or `site/` build output.
- Write opinion into prompts, templates or fixtures' expected articles.
- Edit `config/news_sources.yaml` casually: the outlet list is an editorial-balance decision.
- Raise `news.max_calls_per_run` without saying so: each call is about $0.3 of subscription quota.
- Change JSON schemas in PRD §4 without updating the PRD in the same change.
