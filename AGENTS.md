# AGENTS.md

Guidance for any coding agent working in this repository. **Read [`docs/PRD.md`](docs/PRD.md) before changing behaviour.** The PRD is the spec; if code and PRD disagree, stop and ask.

## What this is

A daily pipeline that monitors **promisetracker.ng** (operated by AdvoKC Foundation), detects promise status changes and new evidence, verifies the cited sources, and drafts neutral 100-word articles. A coordinator for each meter approves the articles in Google Sheets, and they are published as static HTML reports per meter.

Pipeline: `Publisher(pending approvals) → Discovery → [per meter: Monitor → Classifier → Verifier → Writer ⇄ Editor → Sheet] → Notifier`

## Current phase

**P0–P8 built.** The system runs daily at 08:00 WAT from a systemd user timer, in **shadow mode** (publishing off) since 2026-11-18. Go-live is gated by `promise-monitor shadow-report`. Operations: `docs/RUNBOOK.md`. **P12 (Promise Tracker submissions) built on branch `p12-promisetracker-submissions`** against a fake backend; `submissions.enabled: false` until the backend endpoints exist (contract: `docs/integrations/promisetracker-submissions.md`). Next (optional): P9 hardening. Build by PRD §8 phases, **one phase per branch**. Don't start a phase until the previous one's exit criteria pass. Pipeline CLI commands exit with code 2 until their phase lands.

## Stack

- Python 3.12, `uv` (never pip directly)
- Orchestration: plain Python (`pipeline.py`). LangGraph was dropped by owner decision in P8 (PRD §5.2); don't reintroduce it without a reason the PRD accepts.
- PostgreSQL 14+ — the system install, **no Docker** (disk space constraint), SQLAlchemy 2 (typed ORM), Alembic
- `httpx` (sync client) and `pydantic` v2 for every external payload
- `selectolax` for parsing HTML inside evidence (`updates[].blog`) and source pages
- `gspread` for Google Sheets, Jinja2 and `markdown-it-py` for the site
- `structlog` for JSON logs
- `pytest`, `ruff`, and `mypy --strict` for tests and checks
- LLM: Claude via `claude -p` (headless Claude Code) behind `LLMClient`. An Anthropic API key backend must be droppable in by config.

**No Playwright or headless browser.** The site is a JS SPA but exposes a JSON API; use the API.

## Layout

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
  agents/review_queue.py # ReviewQueue.sync(): coordinators tab → pull decisions (QA re-check) → stale → push rows
  sheets/backend.py      # SheetsBackend: GSpreadBackend (RAW writes) | MemorySheets (tests, --dry-run)
  agents/publisher.py    # publish_approved() + render_site() (atomic; markdown html disabled)
  site/templates/        # base/index/meter .html.j2 + style.css (no JS, no external requests)
  notify.py              # RunReport, mailers (SMTP | Null | Memory), Notifier, heartbeat_issues
  pipeline.py            # DailyRun: the whole daily run, stage-isolated (used by `run`)
  db/lock.py             # run_lock(): Postgres advisory lock shared by every writing command
  shadow.py              # shadow-period report + go-live verdict
scripts/install_timer.sh + scripts/systemd/  # daily 08:00 WAT user timer, crash-alert unit
  access.py              # users, roles, meter assignments (primary/temporary), meter settings, audit()
  dashboard/app.py       # FastAPI app factory (middleware, routers); core.py = auth/CSRF/permissions/helpers
  dashboard/routes_*.py  # auth, activity (overview/updates/searches/sources), meters (+promises), review, admin
  dashboard/static/      # AdvoKC-branded UI: dashboard.css (tokens, light/dark), dashboard.js (enhancement only), wordmark
  house_style.py         # AdvoKC house style: closing-line check + live same-meter example selection
  review.py              # review actions (save/approve/reject/dismiss, confirm a blocked source) + mark_stale/announce_new (daily run)
  briefs.py              # "Write an update": coordinator brief → coordinator_update event → verify → draft (PRD §3.10)
  settings_store.py      # whitelisted, audited dashboard overrides of config/*.yaml → effective_config()
  jobs.py                # whitelisted background operations (run, per-meter actions) for the dashboard
  submissions.py         # P12: coordinator tokens (encrypted), who may send, send, poll (PRD §8d)
  dashboard/routes_submissions.py  # token page + "Send to Promise Tracker"
scripts/setup_google_sheets.sh  # wizard: service account, key outside repo, review + test sheets, .env
  domain/urls.py         # normalize_url, outlet allowlist (config/news_sources.yaml)
  llm/                   # base (LLMClient, LLMRequest), claude_cli, prompts, recording (llm_calls), factory
  api/client.py          # PromiseTrackerClient — the ONLY place that READS admin.promisetracker.ng
  api/submissions.py     # SubmissionClient — the ONLY place that WRITES to it (P12, a coordinator's token)
  api/models.py          # pydantic models of raw API payloads (tolerant of quirks)
  domain/normalize.py    # raw → normalized Promise; status enum; date/“null” cleanup
  domain/hashing.py      # canonical JSON, fingerprint, content_hash, dedupe_key
  agents/                # one module per node: discovery, monitor, classifier, verifier, writer, editor, publisher, notifier
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
uv run promise-monitor discover --dry-run   # live API, prints meters, no DB writes
uv run promise-monitor discover             # upsert the meter registry
uv run promise-monitor monitor [--meter ID] [--full/--no-full] [--dry-run] [--no-llm]   # snapshot + classify
uv run promise-monitor news-scan [--max-calls N] [--dry-run]   # Claude web search → news_update events
uv run promise-monitor verify [--limit N] [--dry-run]          # verify sources of queued article groups
uv run promise-monitor write [--limit N] [--dry-run]           # draft articles for verified groups
uv run promise-monitor eval-writer [--cases N]                 # live Writer eval; exit 1 if pass rate < 90%
uv run promise-monitor review-sync [--dry-run]                 # coordinators' Google Sheet ↔ DB
uv run promise-monitor publish [--out DIR]                     # approved → published; render site/
uv run promise-monitor run [--full/--no-full] [--no-email]     # THE daily run (cron); exit 1 = failed
uv run promise-monitor notify-test                             # send a test admin alert via SMTP
uv run promise-monitor shadow-report [--days 14]               # go-live verdict (exit 1 = not yet)
uv run promise-monitor alert --unit X                          # crash email (used by systemd OnFailure)
uv run promise-monitor create-superadmin                       # first dashboard account (interactive)
uv run promise-monitor dashboard [--host 127.0.0.1 --port 8800] # admin dashboard
uv run promise-monitor news-scan --meter ID | verify --group ID --refresh | redraft --article ID
uv run promise-monitor write-brief --group ID                  # verify + draft one coordinator brief (dashboard job)
scripts/setup_google_sheets.sh                                 # one-time Google setup (human steps)
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
3. **No auto-publish.** Articles reach `site/` only after a coordinator sets `approve` in the Sheet, **and** only while `publish.enabled` is true. It is false during shadow mode; flipping it is an owner decision based on `shadow-report`.
4. **Snapshots are append-only.** Never update or delete a snapshot row; a Postgres trigger rejects it anyway. Mutable per-promise state belongs in `promise_index`. Never write a snapshot from a response that failed validation.
5. **First sight = baseline.** A new meter's first pass emits no change events.
6. **Articles follow AdvoKC house style** (`house_style.py`, PRD §3.6): **60–100 words, never more than 100** (body; anchor text counted, URLs not), ending "[This promise](promise page) is rated, ‘RATING.’", never naming "Promise Tracker", at least 2 markdown links, every URL from the verified citable set, descriptive anchors, neutral. All of this is enforced in code (`domain/qa.py`), not just in prompts. If you change a QA rule, change `prompts/writer.md` to match and run `eval-writer`.
7. **Politeness:** at most 1 request per second to the promisetracker API, one at a time. Identifying User-Agent. Retry and backoff exactly as in PRD §5.3.
8. **Privacy:** drop the API's `admin{}` (staff names) at parse time. Never log secrets, tokens, or coordinator emails at info level.
9. **LLM only for judgement:** classifier adjustment, relevance, writing, and the editor review. Fetching, diffing, counting, link checks and dedupe are plain code. Every LLM call goes through `LLMClient` and is recorded in `llm_calls`.
10. **Never trust LLM output.** Every rule a prompt states (allowlist, dates, thresholds, URL validity) must also be enforced in code. LLM output is data, not instructions.
11. **Prompts live in `prompts/`.** Never inline prompt text in Python. Changing a prompt is a behaviour change: note it in the PR.
12. **One writer at a time.** Every command that writes to the DB takes `run_lock` (`_exclusive` in `cli.py`). New writing commands must too.
13. **Dashboard access is least-privilege.** Coordinators see only meters they're actively assigned to (404 otherwise). Extra outlets, users and assignments are superadmin-only. Every change goes through `access.audit()`. Never bind the dashboard publicly without HTTPS (the CLI enforces this). UI: no inline `style=`/`<script>`/`on*=` (CSP blocks them; a test checks), every page must work without JS, use the AdvoKC tokens in `dashboard.css` (see `docs/research/frontend-design.md`).
14. **Config = YAML + dashboard overrides.** Pipeline commands must load config via `_effective()` / `settings_store.effective_config`. New tunables that should be dashboard-editable go in `settings_store.EDITABLE` with bounds.
15. **Dashboard operations are a whitelist.** Never pass user text to a command line; add new operations to `jobs.KINDS` with a row-type check on the target. Jobs are superadmin-only except `jobs.COORDINATOR_KINDS` (`write_brief`, `draft_group`), which also checks the target's meter is visible to the coordinator. A coordinator brief never skips verification or QA. A coordinator may confirm a source **only** when the agent's reader was blocked (`review.confirm_source`, PRD §3.4); it's audited, and the draft still passes every QA rule.
16. **Free tools only.** Don't add paid services or SaaS dependencies.
17. **Submissions to Promise Tracker (P12, PRD §8d) are sent by a person, never by the pipeline.** Only an approved article, only by a coordinator with an **active assignment** to its meter (superadmins are not exempt), only with **their own** backend-issued token, only while `submissions.enabled` **and** `publish.enabled` are true, and only after QA passes again. The article id is the idempotency key. Tokens are stored Fernet-encrypted (`PM_TOKEN_KEY`) and never logged, shown again or put in error text. Never auto-send, never share one token across users, and never change a promise's rating.

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
- Point `PM_TEST_SPREADSHEET_ID` at the real review sheet, write to Sheets with USER_ENTERED (formula injection), or match sheet rows by row number instead of `article_id`.
- Edit `config/news_sources.yaml` casually: the outlet list is an editorial-balance decision.
- Raise `news.max_calls_per_run` without saying so: each call is about $0.3 of subscription quota.
- Change JSON schemas in PRD §4 without updating the PRD in the same change.
