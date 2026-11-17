# PRD — Promise Tracker Monitor

| | |
|---|---|
| Status | Draft v1.0 (agreed in the 2026-11-16 design session) |
| Owner | AdvoKC Foundation, Promise Tracker team |
| Data source | promisetracker.ng public JSON API, used with operator permission |
| Stack | Python 3.12, LangGraph, PostgreSQL, Google Sheets, Jinja2 |
| Schedule | Daily 08:00 WAT (07:00 UTC) |

---

## 0. Problem and goals

promisetracker.ng tracks the promises of Nigerian public officials through **meters**. Each meter holds promises, a status for each, and an evidence timeline. Today, nobody is told when a status changes or new evidence lands. Turning those updates into short news items is manual.

**Goals**
1. Cover every meter on the site and pick up new meters automatically.
2. Detect every *meaningful* change: a status change, new evidence, or a new promise on promisetracker.ng. **Also find outside news reporting concrete developments on a promise (News Scout, §3.9).** Never report the same change or story twice.
3. Turn each meaningful change into a neutral 100-word article (±5 words) with at least 2 verified, descriptively anchored hyperlinks.
4. A human coordinator for each meter approves every article before it is published as a static HTML report per meter.
5. Run unattended every day using free tools only, with a full audit trail.

**Non-goals (v1)**
- Opinion, analysis, or rating promises ourselves. We report what promisetracker.ng published and what the sources show.
- Languages other than English.
- Social-media or email publishing of articles. Email is used only for coordinator and admin alerts.
- Changing data on promisetracker.ng.

---

## 1. Assumptions and open questions

### Confirmed facts (site research, 2026-11-16)
1. The site is a client-side React SPA. Plain HTTP returns an empty `<div id="root">`, so HTML scraping is useless.
2. A public JSON API with no authentication backs the SPA. Base URL: `https://admin.promisetracker.ng/api/v1/promise-tracker`. Response envelope: `{"success": bool, "data": ..., "message": str}`.
3. `admin.promisetracker.ng/robots.txt` = `User-agent: * / Disallow:` (everything allowed). promisetracker.ng has no real robots.txt or sitemap.
4. There are two meter families. Most promise-level fields are shared; the family-specific details are in the table in §4.1.
   - `promise_tracker`: presidential, Senate, House, plus some states (Edo; Osun from 2026-11-27).
   - `promise_ring`: governor meters in about 16 states.
5. Status vocabulary (API values): `kept`, `broken`, `stalled`, `in_the_works`, `compromised`, `not_yet_rated`. Meter count keys use different names (`not_rated`, `in_works`) and come back as **strings**.
6. Evidence lives in `updates[]` on `/view/promise/{id}`. Each update has `id`, `blog` (HTML with `<a href>` evidence links), `status`, `update_type`, `is_published` (string `"1"`), `created_at`. `rating_source_url` is the string `"None"` or null in every sample seen.
7. Public promise page URL: `https://promisetracker.ng/blog/{promise_uuid}`.
8. There is no list-all-meters endpoint, no RSS feed, and no `updated_since` filter.
9. Field quirks: `subject_area` is inconsistent free text (including the literal `"null"`). Dates sometimes hold the placeholder `1970-01-01`. `last_updated_at` is `YYYY-MM-DD HH:MM:SS` while `created_at`/`updated_at` are ISO-8601. `source` is sometimes a URL and sometimes free text ("manifesto").
10. API responses contain staff reporter names (`admin{}`). These are dropped at parse time.
11. `all_meters` entries carry no `meter_type`, and a state's list mixes both families. States without a current tracker (e.g. Ekiti) return an empty `all_meters` even when they have ring meters. The per-kind `tenures` endpoints and `/all/promise-ring/states` are therefore required discovery sources (verified in P1).
12. `meter/{id}/state/{State}` returns promises for both meter families, so every meter has a uniform list endpoint.
13. Live discovery on 2026-11-16 found 23 meters (21 active). They include an archived Anambra tracker with 51 promises, which accounts for the gap between visible meters (358) and the site total (409).

### Assumptions
0. Decisions made while building P2 (2026-11-17):
   - The public rating is `last_published_status`, falling back to `status`.
   - Naive API timestamps (`YYYY-MM-DD HH:MM:SS`) are treated as UTC. They only feed the fingerprint, never an article.
   - A meter whose promises appear for the first time (e.g. Osun going from 0 to N promises) is baselined silently, like a new meter.
   - The anomaly guard counts only changes to promises already known. It trips only when at least `anomaly_min_changes` (5) such promises change **and** they exceed 30% of the meter.
1. AdvoKC permits this internal tool to call its API at the rate in §5.3.
2. The site's `published_status` / `is_published` flags are authoritative. We only act on published content.
3. The 37 state names in config (36 states + FCT) are stable. A meter's state appears in `/state/{name}` or `/all/promise-ring/states`.
4. Using `claude -p` (headless Claude Code, logged in with a subscription) from a cron job on an AdvoKC machine counts as acceptable personal or internal use. The LLM sits behind an interface so an Anthropic API key can replace it without code changes (see risk R-L3).
5. Coordinators can use Google Sheets and receive email.
6. Daily volume is small: about 400 promises, typically 0–20 changes per day.

### Open questions (non-blocking)
1. **Backend asks (nice to have):** a `GET /meters` endpoint and an `?updated_since=` filter. Either one would simplify discovery and monitoring. The system must work without them.
2. Should articles eventually also be published in Hausa, Yoruba, or Igbo? (Out of scope for v1.)
3. Should approved HTML reports be hosted on GitHub Pages or on an AdvoKC domain? (v1 generates locally; hosting is optional.)
4. Who is the default admin for meters that have no assigned coordinator?

---

## 2. System architecture overview

A Python application runs once a day from cron on an AdvoKC Linux machine.

1. It **publishes** any articles that coordinators approved since the last run.
2. It **discovers** every meter through the JSON API and updates the meter registry in PostgreSQL.
3. For each active meter, in priority order, it runs a LangGraph subgraph:
   - **monitors** promises against stored snapshots;
   - **classifies** the changes and scores them;
   - **verifies** the cited sources;
   - **writes** a 100-word draft;
   - **edits/QAs** it;
   - pushes the draft into that meter's tab of the coordinators' Google Sheet.

PostgreSQL is the single source of truth for state and de-duplication. Google Sheets is only the human review queue. The LLM (Claude, via a swappable adapter) is used only for judgement tasks. Fetching, diffing, link checking, counting and de-duplication are plain code.

```
                      cron 07:00 UTC (08:00 WAT)
                                 │
                                 ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ run_daily (LangGraph parent graph)                                │
 │                                                                   │
 │  [Publisher: approved drafts from Sheets → HTML site]             │
 │                 │                                                 │
 │                 ▼                                                 │
 │  [Discovery] ── meter registry (Postgres) ──┐                     │
 │                                             ▼                     │
 │   for each active meter (priority order, sequential):             │
 │   ┌─────────────────────────────────────────────────────────┐     │
 │   │ Monitor → Classifier → Verifier → Writer ⇄ Editor        │     │
 │   │   │           │            │         (≤3 rewrite loops)  │     │
 │   │ snapshots  change_events  sources        │               │     │
 │   └──────────────────────────────────────────┼───────────────┘     │
 │                                              ▼                    │
 │                         review queue (Google Sheet, tab per meter)│
 │                                              │                    │
 │  [Notifier: coordinator digest + admin alerts via Gmail SMTP]     │
 └───────────────────────────────────────────────────────────────────┘
                                                │ coordinator approves
                                                ▼
                           next run (or `make publish`) → Publisher → site/
```

Short form: **Discovery → Monitor → Classifier → Verifier → Writer → Editor → (human review) → Publisher**. In parallel, **News Scout → Classifier** feeds `news_update` events into the same queue (§3.9).

---

## 3. Agent specifications

"Agent" means one LangGraph node with a single responsibility. Nodes marked **[LLM]** call the model; all others are plain code.

### 3.1 Discovery Agent

- **Goal:** keep a complete, current registry of every meter.
- **Inputs:** `config/states.yaml` (37 names), `config/priority.yaml`, the existing `meters` table.
- **Process:**
  1. Call `/presidential`, `/legislative/upper-chamber`, and `/legislative/lower-chamber`. Collect `current_meter` and `all_meters`.
  2. For each state: call `/state/{name}` and `/state/{name}/tenures?kind=promise_tracker|promise_ring`.
  3. Call `/all/promise-ring/states`.
  4. Merge by `meter.id` and upsert into `meters`.
- **Outputs:** a `DiscoveryResult` with `{run_id, new: [meter_id], updated: [meter_id], missing: [meter_id], active: [meter_id]}` plus `meters` rows (schema in §4.1).
- **Rules:**
  - A new meter gets `baseline_pending = true`. Its first Monitor pass stores snapshots only.
  - A meter that was seen before but is absent now gets `registry_status = missing` and the admin is alerted. Meters are never deleted.
  - Non-current tenures get `registry_status = archived` and are not monitored.
  - A meter with no row in the coordinators tab is assigned to the default admin, who is alerted.
- **Tools:** `PromiseTrackerClient` (httpx and pydantic), SQLAlchemy.
- **Failure modes:**
  - Endpoint 5xx or timeout: retry (§5.3). If it still fails, keep previous registry rows for that source and mark the run `degraded`.
  - Validation error (the API changed shape): skip that source and alert the admin.
  - Every source fails: abort the run as `failed` (site down).
- **Handoff:** the list of `active` meter IDs, sorted by `priority_rank` and then by name, goes to the per-meter subgraph.

### 3.2 Monitor Agent

- **Goal:** extract normalized promise data for one meter and detect field-level differences from the last snapshot.
- **Inputs:** `meter_id`, the latest `promise_snapshots` for that meter, and a `force_full` flag (true on the weekly full sweep, Sundays).
- **Process:**
  1. **Cheap pass.** Fetch the meter's promise list (`/meter/{id}/{scope}` or `/all/promise-ring/fetch/{state}`). Build a fingerprint per promise from `status`, `updated_at`, `last_updated_at`, `update_count`, and `published_status`.
  2. **Detail pass.** For each promise whose fingerprint changed, that is new, or when `force_full` is set, fetch `/view/promise/{id}`.
  3. **Normalize.** Map statuses to the enum. Turn `"null"`, `1970-01-01` and `"None"` into `null`. Parse both date formats to UTC. Strip `admin`. Keep only updates with `is_published == "1"`. Extract links from `updates[].blog`, `promise_blog`, and `source` (if it is a URL).
  4. **Hash.** Compute `content_hash = sha256(canonical_json(normalized))`.
  5. **Diff.** If the hash differs from the last snapshot, produce a field diff: `status`, `title`, `description`, `subject_area`, the set of `updates[].id`, and new promise IDs.
- **Outputs:**
  - A new `promise_snapshots` row per changed promise (schema in §4.2).
  - A `RawDiff` per changed promise: `{promise_id, prev_snapshot_id, new_snapshot_id, fields_changed: [str], added_update_ids: [str], is_new_promise: bool}`.
- **Rules:**
  - When `baseline_pending` is set: store snapshots, emit no diffs, then clear the flag.
  - Only published content is considered. If `published_status != "published"`, the promise is ignored for this run.
  - **Anomaly guard:** if more than 30% of a meter's promises change in one run, store no snapshots, emit no diffs, and alert the admin. This is likely an API or data-migration problem.
- **Tools:** `PromiseTrackerClient`, `normalize.py`, SQLAlchemy.
- **Failure modes:**
  - Detail fetch fails: that promise keeps its old snapshot and is retried next run (no false diff).
  - Validation fails for the whole meter: skip the meter and alert.
  - A promise ID disappears: emit `promise_removed` as an internal event. It is logged and alerted, never written up.
- **Handoff:** `RawDiff[]` goes to the Classifier. An empty list ends the subgraph for that meter.

### 3.3 Change Classifier Agent [LLM for borderline cases only]

- **Goal:** label each diff and score how newsworthy it is.
- **Inputs:** `RawDiff[]` plus the before and after snapshots.
- **Labels:**
  - `status_change`: `status` differs.
  - `new_evidence`: one or more new published `updates[].id`.
  - `new_promise`: the promise is new to a meter that has already been baselined.
  - `news_update`: an accepted News Scout finding (§3.9). It is flagged `evidence_lead` so the coordinator can update the tracker.
  - `cosmetic`: only text fields changed, with normalized edit distance < 15% (lowercase, collapse whitespace). Anything above that is `substantive_edit`. Both are logged and never produce an article in v1.
    - *As built (P3):* the planned LLM call for 15–40% edits was dropped. It could not change any outcome, since both labels are logged only, so it would have spent subscription usage for nothing.
- **Newsworthiness score (0–100):**
  - Rule-based base:
    - `status_change`: 70. Add 10 if the new status is `kept`, `broken`, or `compromised`.
    - `new_evidence`: 50.
    - `new_promise`: 45.
    - `news_update`: 55. Add 10 if relevance ≥ 0.9.
    - Add 10 if the meter's `priority_rank` ≤ 3. Add 5 if the promise `priority == high`.
  - LLM adjustment: ±15 at most, with a one-line reason. The final score is capped to 0–100.
    - *As built:* the LLM is called only when its adjustment could flip the article decision, i.e. a non-status article event with a base score within ±15 of the threshold. Status changes and clear-cut scores need no call.
- **Article rule:** `status_change` always gets an article. `new_evidence`, `new_promise` and `news_update` get one when `score ≥ NEWS_THRESHOLD` (default 60). `cosmetic` and `substantive_edit` never do.
- **Grouping:** all events for one promise in one run are merged into one `article_group_id`, so one promise produces one article per run.
- **Outputs:** `change_events` rows (schema in §4.3), each with `classification`, `score`, `score_reason`, `article_group_id`, and `dedupe_key`.
- **De-duplication:** `dedupe_key` has a UNIQUE constraint. If the key already exists, the event is dropped. This is the primary "never report twice" guarantee.
  - *As built:* the key is `sha256(promise_id | type | discriminator)`, where the discriminator is:
    - the new snapshot id for status, new-promise and edit events (each detection is unique, and re-running is safe);
    - the update id for `new_evidence`;
    - the normalized URL for `news_update`.
- **Failure modes:**
  - LLM unavailable: use the rule-based score, set `score_reason = "llm_unavailable"`, and continue.
  - LLM returns invalid JSON: retry once, then fall back the same way.
- **Handoff:** article groups that qualify go to the Verifier. Events that don't qualify are stored with `state = logged`.

### 3.4 Source Verification Agent [LLM for relevance only]

- **Goal:** confirm each candidate source is live, relevant and dated, and decide which ones can be cited.
- **Inputs:** an article group plus candidate URLs: links from new `updates[].blog`, `promise.source` (if it is a URL), and links from `promise_blog`. Only links attached to the change itself (new updates) count as the **required external link**. The others give context.
- **Checks per URL:**
  1. HTTP `HEAD`, then `GET` if needed: follow redirects (max 5), 15 s timeout, 2 retries. Live means a final status of 200–299.
  2. Content type: HTML or PDF. Extract the title, the published date (from meta tags, JSON-LD or `<time>`; for PDFs, metadata), and the first 2,000 characters of text.
  3. Paywall or bot-block heuristics: 401/403/402, known paywall markers, or less than 300 characters of extractable text.
  4. **Relevance [LLM]:** given the promise title and the extracted text or title, the model answers `{relevant: bool, confidence: 0–1, reason}`.
  5. Cache results in `sources` by URL for 7 days.
- **Verdicts:**
  - `verified`: live, relevant (confidence ≥ 0.6), and with a date or a title.
  - `verified_partial`: live, but paywalled or blocked, with a relevant title or date taken from metadata. It can be cited, and the coordinator sees the label.
  - `unverifiable`: live, but relevance can't be established.
  - `broken`: 4xx/5xx or DNS failure after retries.
- The promise page `https://promisetracker.ng/blog/{id}` is always added as a verified source. It is checked as live through the API, since the page itself is the SPA shell.
- **Outputs:** `sources` rows plus `VerificationResult {article_group_id, citable: [source_id], rejected: [{source_id, verdict}], sufficient: bool}`.
- **Sufficiency rule:** the promise page plus at least one external source that is `verified` or `verified_partial`.
- **Failure modes:**
  - Every external source is broken or unverifiable: `sufficient = false`. Skip the Writer, push the group to the Sheet as `needs_sources`, and alert the coordinator.
  - The fetch itself crashes: mark the URL `unverifiable` and continue.
- **Handoff:** groups with `sufficient = true` go to the Writer, along with the citable sources' titles, dates and snippets.

### 3.5 Writer Agent [LLM]

- **Goal:** draft a neutral 100-word article with embedded markdown links.
- **Inputs:** a `WriterInput` JSON:
  - `meter`: name, official, office.
  - `promise`: title, category, promise URL.
  - `changes[]`: type, old status, new status, update text summary.
  - `sources[]`: id, url, title, publisher, date, snippet, verdict.
  - `editor_feedback`: on a retry.
- **Prompt:** `prompts/writer.md` (ready to paste; section 6 summarizes it).
- **Outputs:** `{headline: str, body_markdown: str, sources_used: [source_id]}`.
- **Tools:** the LLM adapter only. No browsing; the Writer may only cite URLs from `sources[]`.
- **Failure modes:**
  - Invalid JSON or empty output: retry once.
  - LLM unavailable: the group stays `pending_draft` and is retried next run.
- **Handoff:** the draft goes to the Editor.

### 3.6 Editor/QA Agent [code checks first, then LLM]

- **Goal:** enforce every acceptance rule before a human sees the draft.
- **Deterministic checks**, which must all pass:
  1. **Word count:** strip markdown link syntax down to the anchor text, split on whitespace, count. It must be 95–105. The headline is excluded.
  2. **Links:** at least 2 markdown links. Every URL must be in the group's `citable` set. No bare URLs. Anchor text must be at least 2 words and not "here", "link", "click", "source", or "this".
  3. **Status wording:** any status mentioned must match the enum display label for the new status.
  4. **De-duplication:** the article group must have no existing `articles` row. The body must have cosine similarity < 0.9 (TF-IDF) with every article for the same promise in the last 90 days.
  5. **Banned words:** an opinion and loaded-language list in `config/banned_terms.yaml` (e.g. "finally", "failed to", "shockingly", "sadly", "commendable").
- **LLM review [LLM]**, which returns `{neutral: bool, factually_consistent: bool, issues: [str]}`:
  - **Neutrality:** no opinion, attribution of motive, praise, or blame.
  - **Factual consistency:** every claim is supported by the `changes[]` data or a cited source's snippet.
- **Loop:** if any check fails, send the list of issues back to the Writer as `editor_feedback`. Allow at most 3 rewrites. After the 3rd failure, push the draft to the Sheet with `qa_flags` set and status `needs_attention`. It is never dropped silently.
- **Outputs:** `articles` row (schema in §4.4) with `state = in_review`, plus a row added to the meter's Sheet tab.
- **Handoff:** the human coordinator.

### 3.7 Publisher Agent

- **Goal:** turn approved drafts into the per-meter HTML reports.
- **Inputs:** coordinator decisions read from the Sheet (`decision` column: `approve`, `edit`, `reject`) and `articles` rows.
- **Process:**
  1. Read every tab and match rows by `article_id`.
  2. **On `approve`:** take the body from the Sheet, since the coordinator may have edited it. Re-run the deterministic Editor checks (word count, links in the citable set, anchor text). If they pass, set `state = approved` and record `approved_by` and `approved_at`. If they fail, write the reason back to the Sheet's `qa_result` column and leave the article `in_review`.
  3. **On `reject`:** set `state = rejected` and store the reason. The change event is now permanently consumed and is never redrafted.
  4. **Expiry:** drafts that get no decision for 7 days become `stale` and the coordinator gets a reminder. Nothing is auto-published.
  5. Render `site/index.html` and `site/meters/{meter_slug}.html` with Jinja2, newest first. Each article shows its headline, its date, its body converted from markdown to HTML with `rel="noopener"` links, the status before and after, and a link to the promise page.
  6. Mark rendered articles `state = published` and set `published_at`.
- **Tools:** gspread, Jinja2, markdown-it-py, and optionally `git push` to GitHub Pages.
- **Failure modes:**
  - Sheets API down: skip publishing this run and alert. The next run catches up.
  - Render error: keep the previous `site/` (atomic swap via a temp directory).
- **Handoff:** none. This is the end state.

### 3.9 News Scout Agent [LLM with web search] (added 2026-11-17)

- **Goal:** find outside news reporting a **concrete development** on a promise, so an article can be written even when promisetracker.ng hasn't changed yet. The finding is also given to the coordinator as an **evidence lead** for updating the tracker.
- **Search engine:** Claude web search through `claude -p` with `--tools WebSearch`. This was the owner's choice over RSS feeds or GDELT.
- **Inputs:** promises that are present on active, baselined meters.
  - The least recently searched go first; meter priority breaks ties.
  - `news.batch_size` (10) promises go into each call, with at most `news.max_calls_per_run` (12) calls.
  - Each promise has a **cutoff**: the later of its last tracker update and today minus `lookback_days` (30).
- **Prompt:** `prompts/news_scout.md`, which embeds the outlet allowlist from `config/news_sources.yaml`.
  - Claude must report only URLs it saw in search results, with full dates.
  - It must list the search queries it ran (audited).
  - It also returns relevant non-concrete items, flagged as such, so editors can see what was filtered out.
- **Code-side gate.** The model is never trusted; every rule is enforced in `agents/news_scout.py`:
  - the promise reference must be one we sent;
  - the URL must normalize, and its outlet must be on the allowlist (exact domain, subdomain, or `*.gov.ng`);
  - the date must be a full date, after the cutoff, and not in the future;
  - it must be `is_concrete_development` with relevance ≥ 0.75;
  - at most 3 per promise;
  - **one story is accepted for one promise only:** the highest relevance wins, and earlier runs win;
  - a URL already stored for the promise is skipped.
- **Outputs:**
  - Every result goes into `news_items`, marked `accepted` or `rejected` with a reason.
  - Accepted items become `NewsFinding`s and then `news_update` events through the Classifier.
  - `news_search_state` records rotation.
- **Framing for the Writer (P5):** report the news with attribution to the outlet, and state the current Promise Tracker rating unchanged. **Never imply the rating changed.** Accepted URLs still go through the Verifier (P4) before any article cites them.
- **Failure modes:**
  - A failed call is not marked as searched, so it goes first next run.
  - An auth failure stops scouting for the run and alerts the admin.
  - An empty result is normal.
- **Usage (measured 2026-11-17):** one 10-promise call took about 15–30 s and used roughly 10–15 web searches, costing about $0.08–0.30 equivalent. 12 calls a day is about $1–4 equivalent per day and covers every promise roughly every 3 days.
- **CLI:** `promise-monitor news-scan [--max-calls N] [--dry-run]`.

### 3.8 Notifier (support component)

- After each run, send each coordinator a daily email digest: new drafts and items that need attention in their meters.
- Send admin alerts for: run failed or degraded, schema validation errors, anomaly-guard trips, missing meters, unassigned meters, Sheets or LLM outages.
- Transport: Gmail SMTP with an app password. If SMTP fails, the error is logged and alerting does not fail the run.

---

## 4. Data model

PostgreSQL is authoritative. The JSON Schemas below describe the records. SQLAlchemy models in `src/promise_monitor/db/models.py` must match them, and Alembic owns migrations.

### 4.1 Meter

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Meter",
  "type": "object",
  "required": ["id", "meter_type", "scope", "name", "registry_status", "first_seen_at", "last_seen_at"],
  "properties": {
    "id": {"type": "string", "format": "uuid", "description": "API meter.id"},
    "meter_type": {"enum": ["promise_tracker", "promise_ring"]},
    "scope": {"enum": ["presidential", "senate", "house", "state"]},
    "state_name": {"type": ["string", "null"], "description": "e.g. 'Akwa Ibom'; null unless scope=state"},
    "name": {"type": "string", "description": "Display name, e.g. 'President Bola Ahmed Tinubu'"},
    "meter_name": {"type": ["string", "null"], "description": "API meter_name, e.g. 'batometer'"},
    "slug": {"type": ["string", "null"]},
    "official_name": {"type": ["string", "null"]},
    "tenure_start": {"type": ["string", "null"], "format": "date"},
    "tenure_end": {"type": ["string", "null"], "format": "date"},
    "is_current": {"type": "boolean"},
    "registry_status": {"enum": ["active", "archived", "missing"]},
    "baseline_pending": {"type": "boolean"},
    "priority_rank": {"type": "integer", "minimum": 1, "description": "1 = highest; default 100"},
    "list_endpoint": {"type": "string", "description": "API path used by Monitor"},
    "public_url": {"type": "string", "format": "uri"},
    "coordinator_email": {"type": ["string", "null"], "format": "email"},
    "promise_count": {"type": "integer"},
    "first_seen_at": {"type": "string", "format": "date-time"},
    "last_seen_at": {"type": "string", "format": "date-time"}
  }
}
```

### 4.2 Promise snapshot

Append-only: a new row is written only when `content_hash` changes, and a Postgres trigger rejects UPDATE/DELETE.

As built, the table holds a few scalar columns for querying (`promise_id`, `meter_id`, `run_id`, `captured_at`, `title`, `status`, `category`, `fingerprint`, `content_hash`). The rest of the schema below is stored as the `content` JSON document, the output of `domain/normalize.py`; diffs are computed from it.

A separate mutable table, `promise_index`, holds each promise's cursor: `latest_snapshot_id`, last `fingerprint`, `content_hash`, `present` and `removed_at`. This keeps snapshots strictly append-only.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "PromiseSnapshot",
  "type": "object",
  "required": ["snapshot_id", "promise_id", "meter_id", "run_id", "captured_at", "title", "status", "content_hash", "fingerprint"],
  "properties": {
    "snapshot_id": {"type": "integer"},
    "promise_id": {"type": "string", "format": "uuid"},
    "meter_id": {"type": "string", "format": "uuid"},
    "run_id": {"type": "string", "format": "uuid"},
    "captured_at": {"type": "string", "format": "date-time"},
    "title": {"type": "string"},
    "description": {"type": ["string", "null"]},
    "category": {"type": ["string", "null"], "description": "normalized subject_area; raw kept in raw_subject_area"},
    "raw_subject_area": {"type": ["string", "null"]},
    "status": {"enum": ["kept", "broken", "stalled", "in_the_works", "compromised", "not_yet_rated"]},
    "priority": {"enum": ["high", "medium", "low", null]},
    "source_text": {"type": ["string", "null"]},
    "promise_url": {"type": "string", "format": "uri"},
    "promise_start_date": {"type": ["string", "null"], "format": "date"},
    "promise_end_date": {"type": ["string", "null"], "format": "date"},
    "last_updated_at": {"type": ["string", "null"], "format": "date-time"},
    "api_updated_at": {"type": ["string", "null"], "format": "date-time"},
    "update_count": {"type": "integer"},
    "updates": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["update_id", "status", "created_at"],
        "properties": {
          "update_id": {"type": "string", "format": "uuid"},
          "status": {"type": "string"},
          "update_type": {"type": ["string", "null"]},
          "created_at": {"type": "string", "format": "date-time"},
          "text": {"type": "string", "description": "blog HTML stripped to plain text"},
          "evidence_links": {"type": "array", "items": {"type": "string", "format": "uri"}}
        }
      }
    },
    "evidence_links": {"type": "array", "items": {"type": "string", "format": "uri"}, "description": "union of all link sources"},
    "fingerprint": {"type": "string", "description": "sha256 of cheap-pass fields"},
    "content_hash": {"type": "string", "description": "sha256 of canonical normalized JSON"}
  }
}
```

### 4.3 Change event

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ChangeEvent",
  "type": "object",
  "required": ["event_id", "run_id", "meter_id", "promise_id", "change_type", "dedupe_key", "detected_at", "state"],
  "properties": {
    "event_id": {"type": "string", "format": "uuid"},
    "run_id": {"type": "string", "format": "uuid"},
    "meter_id": {"type": "string", "format": "uuid"},
    "promise_id": {"type": "string", "format": "uuid"},
    "change_type": {"enum": ["status_change", "new_evidence", "new_promise", "news_update", "cosmetic", "substantive_edit", "promise_removed"]},
    "prev_snapshot_id": {"type": ["integer", "null"]},
    "new_snapshot_id": {"type": ["integer", "null"]},
    "old_status": {"type": ["string", "null"]},
    "new_status": {"type": ["string", "null"]},
    "added_update_ids": {"type": "array", "items": {"type": "string"}},
    "fields_changed": {"type": "array", "items": {"type": "string"}},
    "edit_distance": {"type": ["number", "null"]},
    "score": {"type": "integer", "minimum": 0, "maximum": 100},
    "score_base": {"type": "integer"},
    "score_llm_delta": {"type": "integer", "minimum": -15, "maximum": 15},
    "score_reason": {"type": "string"},
    "article_group_id": {"type": ["string", "null"], "format": "uuid"},
    "dedupe_key": {"type": "string", "description": "UNIQUE; sha256(promise_id|change_type|new_status or update_id)"},
    "detected_at": {"type": "string", "format": "date-time"},
    "state": {"enum": ["logged", "queued", "needs_sources", "drafted", "in_review", "approved", "rejected", "stale", "published"]}
  }
}
```

### 4.4 Article

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Article",
  "type": "object",
  "required": ["article_id", "article_group_id", "meter_id", "promise_id", "headline", "body_markdown", "word_count", "source_ids", "state", "created_at"],
  "properties": {
    "article_id": {"type": "string", "format": "uuid"},
    "article_group_id": {"type": "string", "format": "uuid", "description": "UNIQUE"},
    "meter_id": {"type": "string", "format": "uuid"},
    "promise_id": {"type": "string", "format": "uuid"},
    "event_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    "headline": {"type": "string", "maxLength": 120},
    "body_markdown": {"type": "string"},
    "body_html": {"type": ["string", "null"]},
    "word_count": {"type": "integer", "minimum": 95, "maximum": 105},
    "source_ids": {"type": "array", "minItems": 2, "items": {"type": "integer"}},
    "qa_flags": {"type": "array", "items": {"type": "string"}},
    "rewrite_count": {"type": "integer", "maximum": 3},
    "state": {"enum": ["in_review", "needs_attention", "approved", "rejected", "stale", "published"]},
    "coordinator_email": {"type": ["string", "null"]},
    "decision_note": {"type": ["string", "null"]},
    "approved_by": {"type": ["string", "null"]},
    "approved_at": {"type": ["string", "null"], "format": "date-time"},
    "published_at": {"type": ["string", "null"], "format": "date-time"},
    "created_at": {"type": "string", "format": "date-time"}
  }
}
```

### 4.5 Supporting tables

- `sources`:
  - `source_id`, `url` (UNIQUE, normalized), `final_url`, `http_status`, `verdict`, `title`, `publisher`, `published_date`, `snippet`, `relevance_confidence`, `checked_at`.
  - Join table `article_group_sources(article_group_id, source_id, role: required|context)`.
- `runs`:
  - `run_id`, `started_at`, `finished_at`, `status` (`ok|degraded|failed`), `trigger` (`cron|manual`), `stats` (JSONB: meters, promises fetched, diffs, events by type, drafts, errors).
- `llm_calls`:
  - `id`, `run_id`, `agent`, `event_or_group_id`, `model`, `prompt_sha256`, `prompt_path`, `output` (JSONB), `latency_ms`, `ok`, `created_at`.
- `coordinators`: mirrored from the Sheet's `coordinators` tab (`meter_id`, `name`, `email`, `active`).
- `news_items`:
  - `id`, `run_id`, `promise_id`, `meter_id`, `url` (normalized), `outlet`, `headline`, `published_date`, `summary`, `development_type`, `is_concrete`, `relevance`;
  - `state` (`accepted` or `rejected`) and `reject_reason`;
  - UNIQUE (`promise_id`, `url`).
- `news_search_state`: `promise_id`, `last_searched_at`, `last_run_id` (rotation cursor).
- `change_events` (as built) adds `news_item_id` (FK) and `evidence_lead` (bool) to §4.3. `change_type` gains `news_update`.

### 4.6 Google Sheet layout (review queue)

- Tab `coordinators`: `meter_id | meter_name | coordinator_name | email | active`
- One tab per meter, named with the meter slug. Columns:
  `article_id | created_at | promise_title | change_summary | score | headline | body_markdown | word_count | sources (one per line, with verdict) | qa_flags | decision (approve/edit/reject) | decision_note | qa_result | state`
- The pipeline only appends rows and writes to `qa_result` and `state`. Coordinators only write `body_markdown`, `decision`, and `decision_note`.

---

## 5. Orchestration and scheduling

### 5.1 Cadence
- **Daily:** cron at `0 7 * * *` UTC (08:00 WAT) runs `uv run promise-monitor run`.
- **Weekly full sweep:** Sundays. The Monitor sets `force_full=true` and fetches every promise's detail.
- **Manual commands:** `promise-monitor publish` (Publisher only), `promise-monitor run --meter <id>`, `promise-monitor discover`.
- **Concurrency:** a Postgres advisory lock stops two runs from overlapping.

### 5.2 Sequencing
1. Take the advisory lock and create a `runs` row.
2. Publisher pass (approved drafts → site).
3. Discovery.
4. Per-meter subgraph for each active meter, in priority order, **sequentially**: Monitor → Classifier → Verifier → Writer ⇄ Editor → Sheet. Each meter commits its own transaction. One meter failing never blocks the others.
5. Notifier (digests and alerts).
6. Finalize the `runs` row and release the lock.

LangGraph checkpointing uses the Postgres checkpointer (`langgraph-checkpoint-postgres`) keyed by `run_id`, so a crashed run can resume.

### 5.3 HTTP politeness and retries
- **promisetracker API:**
  - At most 1 request per second, one at a time.
  - `User-Agent: AdvoKC-PromiseMonitor/1.0 (+contact: admin address from config)`.
  - Timeout 20 s.
  - On 5xx, timeouts or connection errors: 3 retries with exponential backoff and jitter (2 s, 4 s, 8 s).
  - On 429: honour `Retry-After`. If it exceeds 60 s, stop fetching for the run and mark it `degraded`.
- **External sources:** at most 1 request per second per host and 4 hosts at once. 2 retries. Same User-Agent.
- **LLM:** 2 retries on transport error. Each `claude -p` call is capped at 120 s.
- **Google Sheets:** 3 retries with backoff, within the free API quota (batched reads and writes).

### 5.4 Human-in-the-loop checkpoints
1. **Every article** needs coordinator approval in the Sheet before publishing. There is no auto-publish path.
2. `needs_sources` and `needs_attention` items are shown to the coordinator with their reasons.
3. The admin reviews anomaly-guard trips, schema errors, and missing or unassigned meters.

### 5.5 Logging and audit
- **structlog** JSON lines go to `logs/YYYY-MM-DD.jsonl`, with `run_id`, `meter_id`, `promise_id` and `agent` bound on every line. Logs are kept for 90 days.
- The **`runs`** and **`llm_calls`** tables provide the full traceability chain: article → events → snapshots → sources → LLM calls → approver.
- Secrets are never logged. Staff names from the API are never stored.

### 5.6 Alerting
Admin email on:
- run `failed` or `degraded`;
- a schema validation error;
- an anomaly-guard trip;
- a missing meter or an unassigned meter;
- the LLM unavailable for a whole run;
- a Sheets outage;
- 2 consecutive failed runs (flagged as urgent).

---

## 6. Writer Agent system prompt

The full, ready-to-paste prompt is in **[`prompts/writer.md`](../prompts/writer.md)**. It is version-controlled, and its SHA-256 is recorded on every `llm_calls` row. Key constraints:
- 100 words (95–105), headline excluded.
- At least 2 markdown links, using only the URLs provided.
- Descriptive anchors.
- Strictly neutral; attribute claims to sources.
- Output is JSON only.

Other prompts live alongside it: `prompts/classifier.md`, `prompts/relevance.md`, `prompts/editor.md`.

---

## 7. Sample output

> ⚠️ **ILLUSTRATIVE — not a real update, not for publication.** It uses the structure of a real House of Representatives promise, but the change and the "Votes and Proceedings" source are invented. The `example.org` URL is a placeholder.

**Change event**

```json
{
  "event_id": "7d1b0c52-4f0e-4a59-9a3e-1e2f6c0b9a11",
  "run_id": "0f4e2b8a-3c1d-4e5f-8a9b-1c2d3e4f5a6b",
  "meter_id": "132756fd-062e-43c1-ab34-4f7e4ba036cd",
  "promise_id": "031e8e3b-f198-439d-9038-c7a2874eb19f",
  "change_type": "new_evidence",
  "old_status": "in_the_works",
  "new_status": "in_the_works",
  "added_update_ids": ["c5a7e0d2-1111-4b2c-9d3e-000000000001"],
  "fields_changed": ["updates", "update_count", "last_updated_at"],
  "score": 70,
  "score_base": 60,
  "score_llm_delta": 10,
  "score_reason": "Bill advanced a legislative stage; first movement since April 2025.",
  "article_group_id": "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "dedupe_key": "sha256(031e8e3b…|new_evidence|c5a7e0d2…)",
  "detected_at": "2026-11-17T07:03:12Z",
  "state": "in_review"
}
```

**Article** (body = 100 words, headline excluded)

**Headline:** CAMA amendment bill on civil society barriers passes second reading in House

The House of Representatives' promise to review the Companies and Allied Matters Act (CAMA) to remove barriers facing civil society organisations has gained new evidence on Promise Tracker. Its status remains In the Works. According to the [House Votes and Proceedings of 12 November 2026](https://example.org/illustrative/votes-and-proceedings), the CAMA (Amendment) Bill, HB 659, passed second reading and was referred to the Committee on Commerce for further legislative action. The commitment comes from the [Legislative Agenda of the 10th House](https://placng.org/i/wp-content/uploads/2023/11/Legislative-Agenda-of-the-10th-House-of-Representatives.pdf). The committee has not yet submitted its report to plenary. The full evidence timeline is on the [Promise Tracker page for this promise](https://promisetracker.ng/blog/031e8e3b-f198-439d-9038-c7a2874eb19f).

---

## 8. Implementation roadmap

Each phase ends with passing tests and a short demo. One phase at a time.

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 – Skeleton** | uv project, ruff/mypy/pytest, config loading, `.env`, system Postgres (no Docker), Alembic baseline, structlog | `make check` is green; `promise-monitor --help` works |
| **P1 – API client + Discovery** | `PromiseTrackerClient` with pydantic models from the saved fixtures, rate limiter and retries; Discovery node; `meters` table | Discovery against the live API lists every known meter; fixture tests cover schema quirks |
| **P2 – Monitor + snapshots** | normalization, fingerprint/hash, diffing, baseline mode, anomaly guard | 2 runs on unchanged data give 0 diffs; injected fixture changes give the correct `RawDiff`s |
| **P3 – Classifier + dedupe + News Scout** | rules, edit distance, grouping, `dedupe_key`; LLM adapter (`claude -p`) plus `llm_calls` logging; News Scout (Claude web search, code-side gate, rotation) | Re-running the same diff creates no new event; the cosmetic fixture is filtered out; a live news scan stores audited results ✅ 2026-11-17 |
| **P4 – Verifier** | link fetch, metadata/date extraction, paywall heuristics, relevance LLM, source cache | Fixture URLs (live, 404, paywall, PDF) get the correct verdicts |
| **P5 – Writer + Editor** | prompts, the deterministic QA suite, the rewrite loop | ≥ 90% of fixture groups pass QA within 3 tries; every QA rule has a unit test |
| **P6 – Sheets review loop** | gspread sync, coordinators tab, approve/edit/reject/stale handling | An end-to-end test using a test spreadsheet |
| **P7 – Publisher + Notifier** | Jinja2 site, atomic render, Gmail digests and alerts | `site/` renders; an alert email arrives on an injected failure |
| **P8 – Production** | cron, LangGraph Postgres checkpointer, advisory lock, log rotation, runbook, 2-week shadow run (drafts reviewed, not published) | 14 consecutive daily runs with 0 duplicate events and 0 unhandled exceptions |
| **P9 – Hardening (optional)** | API-key LLM backend, GitHub Pages deploy, backend asks (`/meters`, `updated_since`), more languages | As agreed with the team |

---

## 9. Risks and mitigations

| ID | Area | Risk | Mitigation |
|---|---|---|---|
| R-L1 | Legal | Republishing site content ("All Rights Reserved") | Internal AdvoKC tool used with operator permission; articles are original 100-word summaries that link back to the source |
| R-L2 | Legal | Quoting copyrighted third-party sources | Articles only paraphrase and link; no quotes over 15 words; snippets are stored only for verification, never published |
| R-L3 | Legal/Tech | Using a subscription OAuth token for unattended automation may fall outside Anthropic's consumer terms; the token can expire | The LLM sits behind an adapter interface; switching to an API key is a config change; on auth failure, alert the admin and fall back to rule-based scoring (drafts wait) |
| R-L4 | Privacy | Staff names in API responses | Dropped at parse time; never stored, logged, or published |
| R-E1 | Ethical/Editorial | Partisan tone or loaded wording | Neutral prompt, a banned-terms list, an LLM neutrality check, and mandatory human approval |
| R-E2 | Editorial | Hallucinated facts or links | Writer gets only verified sources; Editor checks every URL against the citable set and the claims against the snippets; coordinator review |
| R-E3 | Editorial | Reporting unpublished or draft ratings | Only published content is processed (§3.2) |
| R-E4 | Editorial | Duplicate articles | UNIQUE `dedupe_key`, a UNIQUE `article_group_id` on articles, and a similarity check against the last 90 days |
| R-T1 | Technical | API shape changes without notice | pydantic validation; meters that fail validation are skipped with an alert; snapshots are never overwritten with bad data |
| R-T2 | Technical | Bulk data edits on the site look like hundreds of changes | Anomaly guard (>30% of a meter changes means no drafts, alert) |
| R-T3 | Technical | Site or API downtime | Retries and backoff; the run is marked failed; the next run catches up because state comes from snapshots, not time windows |
| R-T4 | Technical | Missed changes when the cheap pass fingerprint misses an edit | Weekly full detail sweep |
| R-T5 | Technical | Dead source links | Verifier verdicts; broken links are never cited; `needs_sources` goes to the coordinator |
| R-T6 | Operational | A single machine goes down and cron doesn't run | Catch-up is automatic; an admin alert after 2 missed or failed runs (heartbeat check at the start of each run) |
| R-T7 | Operational | Coordinators ignore the queue | 7-day stale reminder; a pending count in the digest; drafts are never auto-published |
| R-T9 | Cost | News Scout web searches use subscription quota (about $0.3 equivalent per 10-promise call) | Trimmed CLI calls (~1k-token system prompt), batching, a calls-per-run cap with rotation, `news.enabled` switch; usage is visible in `llm_calls.cost_usd` |
| R-E5 | Editorial | The News Scout invents or misdates a URL, or uses a partisan source | Allowlist enforced in code; full dates only; audit of `search_queries`; URL verified live by the Verifier (P4) before citation; coordinator approval |
| R-E6 | Editorial | One story produces several near-duplicate articles across promises | Each URL is accepted for one promise only (highest relevance; earlier runs win) |
| R-T8 | Cost | "Free tools only" | Postgres (local), Sheets, Gmail SMTP, cron, and GitHub Pages are all free; the LLM goes through the existing subscription |
