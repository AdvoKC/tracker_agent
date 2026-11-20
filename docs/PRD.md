# PRD — Promise Tracker Monitor

| | |
|---|---|
| Status | Draft v1.0 (agreed in the 2026-11-16 design session) |
| Owner | AdvoKC Foundation, Promise Tracker team |
| Data source | promisetracker.ng public JSON API, used with operator permission |
| Stack | Python 3.12 (plain, stage-isolated pipeline; LangGraph dropped in P8, see §5.2), PostgreSQL, Google Sheets, Jinja2, Claude via `claude -p` |
| Schedule | Daily 08:00 WAT (07:00 UTC) |

---

## 0. Problem and goals

promisetracker.ng tracks the promises of Nigerian public officials through **meters**. Each meter holds promises, a status for each, and an evidence timeline. Today, nobody is told when a status changes or new evidence lands. Turning those updates into short news items is manual.

**Goals**
1. Cover every meter on the site and pick up new meters automatically.
2. Detect every *meaningful* change: a status change, new evidence, or a new promise on promisetracker.ng. **Also find outside news reporting concrete developments on a promise (News Scout, §3.9).** Never report the same change or story twice.
3. Turn each meaningful change into a neutral update article **in AdvoKC's house style, never more than 100 words** (60–100; owner decision 2026-11-18, replacing "100 ± 5"), with at least 2 verified, descriptively anchored hyperlinks.
4. A human coordinator for each meter approves every article before it is published as a static HTML report per meter.
5. Run unattended every day using free tools only, with a full audit trail.

**Non-goals (v1)**
- Opinion, analysis, or rating promises ourselves. We report what promisetracker.ng published and what the sources show.
- Languages other than English.
- Social-media or email publishing of articles. Email is used only for coordinator and admin alerts.
- Changing data on promisetracker.ng directly. *Amended 2026-11-19 (owner):* approved updates may be **submitted to its review queue** with a coordinator's own token (§8d). Promise Tracker editors still publish, and ratings are never changed.

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

**As built (P4, 2026-11-17):**
- **Blocked sites.** About half the allowlisted outlets answer 403 or 401 to an honestly identified fetcher (Channels TV, TheCable, Vanguard, Guardian NG, Reuters). Claude's WebFetch is blocked too.
  - A blocked or paywalled URL is therefore confirmed only when it appears in **raw WebSearch tool results**, which are taken from the CLI's `stream-json` events and are not written by the model.
  - News URLs reuse the News Scout's grounding. Any other URL gets one search call. A hit is cached for `cache_days`; a miss is retried after `search_retry_days` (default 1), because new articles reach search late. A hit counts when its normalized URL matches, or when it has the same host and the same long final path segment (≥ 20 characters, e.g. an AMP copy).
  - Confirmed and relevant means `verified_partial`. Not confirmed means `unverifiable`.
  - **Coordinator confirmation (added 2026-11-19, owner decision).** The search index misses some outlets entirely (Channels TV in Nov 2026), so a blocked page that search can't find is **not rejected**. The lead shows it as "agent blocked: confirm". A coordinator who can see the meter opens the page, ticks "I opened this page. It's genuine and reports this development", and can correct the headline. The source becomes `verified_partial` and citable, with `confirmed_by`/`confirmed_at`/`confirmed_title` on `article_group_sources` and a `source.confirm` audit row. If the group is then sufficient (same rule as below), it goes back to `verified` and a `draft_group` job drafts it. The draft passes every QA rule and is approved as usual. A confirmation outranks the title-only relevance guess and survives re-verification. Only blocked or paywalled pages can be confirmed; readable pages keep their checked verdicts.
- **Relevance input:** for `new_evidence` and `status_change`, the judge also gets the published Promise Tracker update text (≤ 800 characters each), because that text is what ties a cited article to the promise.
- **Required sources per event:**
  - `new_evidence`: the new update's links;
  - `status_change` with no new update: the latest update's links;
  - `new_promise`: its source and write-up links;
  - `news_update`: the news URL.

  Context sources are the promise's own links, up to `max_context_sources`.
- **News dates:** a fetched page dated more than `date_tolerance_days` before the News Scout's claimed date is `unverifiable` (`date_mismatch`).
- **News events:** a `news_update` whose own URL isn't citable becomes `needs_sources` on its own. The rest of the group can still proceed.
- **Group state** lives in `article_groups`: `queued` → `verified` / `needs_sources`. Per-source verdicts are in `article_group_sources`, and the fetch cache is in `sources`.
- **SafeFetcher** refuses non-public addresses on every redirect hop (SSRF guard), and caps redirects, time and bytes.
- **First live result:** the unconfirmable Channels TV "16 Nov" port link was refused. A grounded Channels TV rail story verified as `verified_partial`.
- **CLI:** `promise-monitor verify [--limit N] [--dry-run]`.

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

**As built (P5, 2026-11-17):**
- **Deterministic QA** (`domain/qa.py`), run before any LLM review:
  - **Word count:** anchor text counts, URLs don't. A word is a token containing a letter or digit.
  - **Headline:** at most 14 words and 120 characters; no `?`, `!` or links.
  - **Links:** at least 2, every one in the citable set (compared after normalization). The promise page and at least one external source are required. Anchors must be descriptive, and no URL may appear outside a link.
  - **Status labels:** only the current and old rating may appear, and the current rating must be stated.
  - **News or evidence-only groups:** wording that implies a rating change is banned ("moved from", "now rated", …).
  - **Banned terms:** matched as whole words.
  - **Quotes:** at most 15 words.
  - **Near-duplicates:** cosine similarity ≥ 0.9 against the same promise's articles from the last 90 days.
- **LLM editor review** runs only after QA passes, to save usage. It checks neutrality and factual consistency against the writer input.
- **Loop:** `run_draft_loop` writes, checks, then rewrites using the issues as feedback, at most 3 times. The draft with the fewest issues is kept.
  - Pass: the article is `in_review`.
  - No pass after 3 rewrites: the article is `needs_attention`, with `qa_flags`.
  - The group and its events become `drafted`.
  - If the LLM is unavailable, the group stays `verified` for the next run.
- **Audit:** `articles.writer_input` stores exactly what the Writer was given.
- **CLI:** `promise-monitor write [--limit N] [--dry-run]` and `promise-monitor eval-writer` (the P5 exit check: ≥90% of fixture cases pass within 3 rewrites).

**AdvoKC house style (2026-11-18, owner request):** articles must read like AdvoKC's own updates. The conventions were studied from 462 AdvoKC evidence updates (`house_style.py`):
- **Headline:** the actor and the action, following the meter's own capitalisation (presidential updates use UPPERCASE, state meters Title Case). Bill updates lead with the bill number.
- **Lead:** actor first, with concrete figures, places and dates.
- **Attribution:** "a statement by…", "according to a report by…", linked to the source the evidence cites.
- **Closing line, always:** "[This promise](promise page) is rated, ‘IN THE WORKS.’", with the rating in capitals; "now rated" only for a status change.
- **Never "Promise Tracker":** that phrase appears in 0 of 462 AdvoKC updates.
- **Length:** median 74 words, so the limit is **60–100, target 80**.

How it is applied:
- Each draft gets **up to 4 real AdvoKC updates from the same meter**, then the same meter family, most recent first, as style examples. They are selected live from the snapshots, so the style follows AdvoKC's latest writing.
- The code QA enforces the closing line, the ban on "Promise Tracker" and the word limit.
- The Editor accepts the house voice (evidence facts stated directly; crediting a source whose title or snippet contains the fact), but still flags copied evaluative words and any added purpose, effect or total.
- **Eval:** `promise-monitor eval-writer` scored 9 of 9 at 62–97 words, using `tests/fixtures/house_style_examples.json` (41 real AdvoKC updates).

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
  - `news.batch_size` (15) promises go into each call, with at most `news.max_calls_per_run` (5) calls: about 75 promises a day, a full rotation about every 5 days (lowered from 10 × 12 on 2026-11-20 to save quota; the rotation must stay well inside `lookback_days` so no news is missed).
  - Each promise has a **cutoff**: the later of its last tracker update and today minus `lookback_days` (30).
- **Prompt:** `prompts/news_scout.md`, which embeds the outlet allowlist from `config/news_sources.yaml`.
  - Claude must report only URLs it saw in search results, with full dates.
  - It must list the search queries it ran (audited).
  - It also returns relevant non-concrete items, flagged as such, so editors can see what was filtered out.
- **Code-side gate.** The model is never trusted; every rule is enforced in `agents/news_scout.py`:
  - the promise reference must be one we sent;
  - **grounding:** the URL must appear in the raw WebSearch tool results of that same call (added in P4, after a live run produced a Channels TV URL that no search could find);
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

**As built (P7, 2026-11-17):**
- `publish_approved` moves only `approved` articles, and their group and events, to `published`.
- `render_site` writes `index.html` and `meters/<slug>-<id8>.html`, newest first. Each page shows the rating badge (with the old rating for status changes), a "news report" label and a Sources list. A `verified_partial` source is described as "the site blocks automated reading; its headline was confirmed through search".
- Markdown is rendered with raw HTML **disabled**. Jinja2 autoescapes everything else. Every link gets `rel="noopener noreferrer"`. No JavaScript, no external requests, and dark mode is supported.
- The site is built in a temp directory (mode 755) and swapped in. A failed render leaves the previous site.
- **CLI:** `promise-monitor publish [--out DIR]`. Serving or hosting `site/` (GitHub Pages or an AdvoKC domain) is P9.

### 3.10 Coordinator briefs: "Write an update" (added 2026-11-18)

A coordinator (for their meters) or the superadmin can ask for an update on any promise still on the tracker, from its dashboard page.
- **Input:** a description of what happened (30–1500 characters), the rating the update reports (default: the current rating), and 1–5 source links.
- **Record:** a `coordinator_update` change event in its own queued article group (`briefs.py`), audited. Only one brief per promise may be in progress at a time.
- **Same road as a detected change:** the dashboard starts the `write_brief` job (`promise-monitor write-brief --group ID`). The Verifier treats the links as REQUIRED sources (fetch, search-confirm, relevance, sufficiency). No citable source means an evidence lead, never a draft. Then Writer ⇄ Editor with every QA rule, and the draft goes to the review queue for approval. If the job can't run, the daily run picks the group up.
- **Writer input:** `{"type": "coordinator_update", "description": …}`. If the chosen rating differs from the tracker's, a `status_change` is added and the closing line says "is now rated". The article may state only facts that the description states AND a cited source supports. The description is data, never instructions. The coordinator is never named.
- **Access:** `write_brief` is the only job a coordinator may start, and only for a group on a meter they can currently see (`jobs.COORDINATOR_KINDS`).
- A re-rating in an article does not change Promise Tracker: the coordinator still updates the rating there.

### 3.8 Notifier (support component)

- After each run, send each coordinator a daily email digest: new drafts and items that need attention in their meters.
- Send admin alerts for: run failed or degraded, schema validation errors, anomaly-guard trips, missing meters, unassigned meters, Sheets or LLM outages.
- Transport: Gmail SMTP with an app password. If SMTP fails, the error is logged and alerting does not fail the run.
- *As built (P7):* `notify.py` holds the `RunReport` that every stage fills in.
  - **Coordinator digest:** one email per coordinator per run, sent only when there are new rows or stale drafts. Unassigned meters go to the admin.
  - **Admin alert:** one email per run, sent when the run is degraded or failed, or has warnings or urgent items. "URGENT" covers 2 consecutive failed runs, a last good run more than 36 hours ago, or a Claude login failure.
  - With no SMTP settings, `NullMailer` records skipped emails. `promise-monitor notify-test` sends a test alert.

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

*As built:* `word_count` has no database CHECK, because `needs_attention` drafts may be out of range. The table also has `writer_input` (JSON, audit). `article_group_id` is UNIQUE and references `article_groups`.

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
- `news_items` (as built) also has `search_grounded` and `search_title`. The latter is the title from the raw search result.
- `sources` (as built):
  - `fetch_status` is one of `ok`, `blocked`, `paywalled`, `broken`, `unsupported`, `unsafe`, `error`;
  - it also has `search_confirmed`, `search_title`, `search_checked_at` and `error`.
- `article_groups`:
  - `group_id`, `meter_id`, `promise_id`;
  - `state`: `queued`, `verified`, `needs_sources`, `drafted`, `in_review`, `published` or `rejected`;
  - `sufficient`, `notes`, `verified_at`.
- `article_group_sources`:
  - (`group_id`, `source_id`), `role` (`promise_page`, `required` or `context`), `event_id`;
  - `relevant`, `relevance_confidence`, `reason`, `verdict`, `citable`.
- `change_events` (as built) adds `news_item_id` (FK) and `evidence_lead` (bool) to §4.3. `change_type` gains `news_update`.
- `change_events` (migration 0011, 2026-11-18): `change_type` gains `coordinator_update`, with `brief` (text), `brief_urls` (JSON list) and `requested_by` (FK users). See §3.10.

### 4.6 Google Sheet layout (review queue)

- Tab `coordinators`: `meter_id | meter_name | coordinator_name | email | active`
- One tab per meter, named with the meter slug. Columns:
  `article_id | created_at | promise_title | change_summary | score | headline | body_markdown | word_count | sources (one per line, with verdict) | qa_flags | decision (approve/edit/reject) | decision_note | qa_result | state`
- The pipeline only appends rows and writes to `qa_result` and `state`. Coordinators only write `body_markdown`, `decision`, and `decision_note`.

*As built (P6, 2026-11-17):*
- **Columns:** `article_id | kind | created_at | promise_title | change_summary | score | evidence_lead | headline | body_markdown | word_count | sources | qa_flags | decision | decision_note | qa_result | state`.
- **Tabs** are named `<meter name> · <first 8 chars of meter id>` and found by that suffix.
- **Coordinators** may edit `headline` as well as `body_markdown`.
- **Decisions** are `approve`, `edit` or `reject`. Lead rows take `dismiss`.
  - `edit` re-checks the edited text without approving.
  - `approve` re-runs the deterministic QA on the sheet text first.
  - The pipeline clears `decision` once it has acted on it, and also writes `word_count`.
- **`kind=lead` rows** are `needs_sources` groups: evidence leads for updating the tracker.
- **Rows are matched by `article_id`**, never by row number.
- **All writes use RAW input**, so text is never evaluated as a formula.
- **Approver:** we can't see who edited a cell, so `approved_by` is the meter's coordinator email from the `coordinators` tab.
- **CLI:** `promise-monitor review-sync [--dry-run]`. Setup is `scripts/setup_google_sheets.sh`.

---

## 5. Orchestration and scheduling

### 5.1 Cadence
- **Daily:** cron at `0 7 * * *` UTC (08:00 WAT) runs `uv run promise-monitor run`.
- **Weekly full sweep:** Sundays. The Monitor sets `force_full=true` and fetches every promise's detail.
- **Manual commands:** `promise-monitor publish` (Publisher only), `promise-monitor run --meter <id>`, `promise-monitor discover`.
- **Concurrency:** a Postgres advisory lock stops two runs from overlapping.
  - *As built:* `db/lock.py` is shared by every writing command. If it's busy, `run` exits 0 ("skipping") and the other commands exit 3.
- **Scheduler (as built):** a systemd **user** timer.
  - `OnCalendar=*-*-* 08:00:00 Africa/Lagos` with `Persistent=true`, so a run missed while the machine was off happens at the next boot.
  - `OnFailure` sends a crash email.
  - Exit code 2 means "failed but already reported", so it doesn't trigger a second alert.
  - Install with `scripts/install_timer.sh`. Operations are in `docs/RUNBOOK.md`.
- **Shadow mode (as built):** `publish.enabled: false`. Approvals are held and nothing is published. `promise-monitor shadow-report` gives the go-live verdict.
- **Log retention:** `logs.retention_days` (90). Pruned at the start of each run.

### 5.2 Sequencing

*As built (P7):* `pipeline.DailyRun` runs `heartbeat → review_pull → publish → discover → monitor(+classify) → news(+classify) → verify → write → review_push → notify`.
- Each stage commits on its own, and a stage that raises is recorded without stopping the run.
- If the API is down, discovery marks the run `failed` and monitoring is skipped. Publishing, review sync and alerts still happen.
- `promise-monitor run [--full/--no-full] [--no-email]` exits 1 on `failed`.
- The advisory lock and the LangGraph or checkpoint question are P8.

1. Take the advisory lock and create a `runs` row.
2. Publisher pass (approved drafts → site).
3. Discovery.
4. Per-meter subgraph for each active meter, in priority order, **sequentially**: Monitor → Classifier → Verifier → Writer ⇄ Editor → Sheet. Each meter commits its own transaction. One meter failing never blocks the others.
5. Notifier (digests and alerts).
6. Finalize the `runs` row and release the lock.

~~LangGraph checkpointing uses the Postgres checkpointer keyed by `run_id`, so a crashed run can resume.~~

**Decision (P8, 2026-11-17, owner-approved): no LangGraph.** The pipeline is a straight sequence with no branching or cycles between agents, so a graph framework adds a dependency and a checkpoint store for little gain. Crash recovery comes from idempotency instead:
- append-only snapshots plus the `promise_index` cursor;
- the UNIQUE `dedupe_key` and `article_group_id`;
- group and article states;
- the run lock.

Re-running after a crash continues where the last run stopped, and nothing is reported twice. Revisit this if agents ever need branching or human-in-the-loop pauses inside a run.

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

The full, ready-to-paste prompt is in **[`prompts/writer.md`](../prompts/writer.md)**. Since P5 it uses the system/`---USER---` format and states the exact word-counting rule the code enforces. The Editor's prompt is `prompts/editor.md`. It is version-controlled, and its SHA-256 is recorded on every `llm_calls` row. Key constraints:
- 60–100 words, never more than 100, headline excluded (AdvoKC house style, §3.6).
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
| **P4 – Verifier** | link fetch, metadata/date extraction, paywall heuristics, relevance LLM, source cache; raw-search grounding for blocked URLs and News Scout results | Fixture URLs (live, 404, paywall, PDF) get the correct verdicts ✅ 2026-11-17 |
| **P5 – Writer + Editor** | prompts, the deterministic QA suite, the rewrite loop | ≥ 90% of fixture groups pass QA within 3 tries; every QA rule has a unit test |
| **P6 – Sheets review loop** | gspread sync, coordinators tab, approve/edit/reject/stale handling | An end-to-end test using a test spreadsheet ✅ in-memory e2e 2026-11-17; live round trip runs once the setup wizard is done |
| **P7 – Publisher + Notifier** | Jinja2 site, atomic render, Gmail digests and alerts; `promise-monitor run` daily pipeline | `site/` renders; an alert email arrives on an injected failure ✅ tested in-memory 2026-11-17; a live email needs SMTP settings (`notify-test`) |
| **P8 – Production** | systemd timer (instead of cron), advisory lock, log retention, runbook, crash alert, 2-week shadow run (drafts reviewed, not published); LangGraph dropped by decision | 14 consecutive daily runs with 0 duplicate events and 0 unhandled exceptions: **in progress**, shadow period started 2026-11-18; check with `shadow-report` |
| **P9 – Hardening (optional)** | API-key LLM backend, GitHub Pages deploy, backend asks (`/meters`, `updated_since`), more languages | As agreed with the team |
| **P12 – Promise Tracker submissions** | Coordinator tokens, "Send to Promise Tracker" on approved articles, submission status tracking, self-update suppression (§8d) | Fake-backend e2e green; one live submission to a test promise once the backend endpoints exist |

---

## 8b. Admin dashboard (P10, added 2026-11-17)

**Goal:** fit the agents to each meter, and give AdvoKC staff a view of each meter's promises, with per-person access.

**Decisions (owner, 2026-11-17):**
- **Customisable per meter:** news-search context only.
  - Official/office names and aliases, extra search terms, exclude terms.
  - Extra local outlets, **superadmin only** (editorial balance).
- **Promises:** the meter's promises, already captured by the Monitor, are shown read-only and given to the agents as **meter-wide context**.
  - The News Scout searches **one meter per call**, with that meter's context and full promise list.
  - The meter's extra outlets are allowed **for that meter only**, enforced in code.
- **Hosting:** a local web app (FastAPI, server-rendered, no JavaScript) bound to `127.0.0.1:8800`. VPS deployment comes later; then it goes behind an HTTPS reverse proxy with `PM_DASHBOARD_HTTPS=true`.
- **Roles:**
  - The **superadmin** owns everything: users, primary coordinator per meter, **temporary access** grants (start/end in WAT plus a reason; they expire by themselves and can be revoked) and extra outlets.
  - A **coordinator** sees **only** meters they're actively assigned to, primary or temporary-in-window, and edits their search context. Other meters return 404.
- **Coordinator routing:** dashboard assignments are the source of truth for a meter once set.
  - The digest goes to the primary coordinator **and** any active temporary cover.
  - The sheet's `coordinators` tab mirrors them. It remains the source of truth only for meters with no dashboard assignment.

**Data:** `users` (scrypt password hashes), `meter_assignments` (primary or temporary, with window, reason, granted_by, revoked_at), `meter_settings`, and an append-only `audit_log` (who, when, before → after) for every dashboard change.

**Security:**
- signed session cookie (HttpOnly, SameSite=Strict, Secure under HTTPS), 8-hour lifetime, new session on login;
- CSRF token on every form; strict CSP (same-origin script and style only, nothing inline; JS is progressive enhancement); no framing; no caching; API docs disabled;
- lockout after 5 failures for 15 minutes; new users get a one-time password they must change;
- **the server refuses non-local binding without HTTPS and a secret.**

**CLI:** `promise-monitor create-superadmin` and `promise-monitor dashboard [--host --port]`.

### 8c. Full management dashboard (P11, 2026-11-17)

**Owner decisions:** review moves **into the dashboard only** (`review.channel: dashboard`); the Google Sheet queue (§4.6, P6) is kept as a legacy channel (`sheets`) but no longer used. A superadmin can **run the pipeline now**, start **per-meter actions** and **edit global settings**.

**Pages:**
- **Overview:** open drafts, items needing attention, leads, held approvals, the week's changes and news, latest runs with warnings and errors, Claude cost, jobs.
- **Review queue:** filter by meter or state.
  - **Draft page:** what changed, QA issues, a preview, an editable headline and body, Save & re-check / Approve / Reject, and every source with its fetch result, verdict, relevance and reason.
  - **Lead page:** why no draft was written, the news behind it, and Dismiss.
- **Meters → meter → promise:** a promise's evidence timeline, snapshot history, detected changes, articles and news found.
- **Activity:** **Updates** (every change event, filterable), **Searches** (each News Scout call with the queries it ran and every result with its accept/reject reason), **Sources** (every checked page and where it's used).
- **Admin:**
  - **Runs**, with details and Claude calls;
  - **Jobs**, with output;
  - **Settings**: whitelisted, audited overrides layered on the YAML;
  - **Users**, with a page per user for multi-meter primary assignment, temporary cover, role, enable/disable and password reset;
  - **LLM usage** and **Audit**.

**Rules kept:**
- Approve re-runs the code QA on the current text, and nothing auto-publishes (shadow mode still holds).
- Coordinators get 404 for other meters' drafts, leads, promises, searches and sources.
- Operations run a **fixed whitelist** of `promise-monitor` commands as separate processes. Targets are validated row ids. They use the same run lock; "busy" means the lock was held.
- Settings changes are range-checked, and the whole config is re-validated before saving.

**As built:**
- `review.py` holds the review actions, shared with the daily run: `mark_stale` and `announce_new` drive the digests, which link to `review.dashboard_url`.
- `settings_store.py` handles the overrides, applied by every pipeline command (`effective_config`).
- `jobs.py` handles the operations.
- New CLI options: `news-scan --meter`, `verify --group --refresh`, `redraft --article`.

### 8d. Promise Tracker submissions (P12, added 2026-11-19)

**Goal:** let a coordinator send an approved article to promisetracker.ng's own review queue from our dashboard, without logging into the Promise Tracker admin. The backend contract is in [`docs/integrations/promisetracker-submissions.md`](integrations/promisetracker-submissions.md). It is a **proposal**: the backend endpoints may not exist yet.

**Owner decisions (2026-11-19):**
- **The backend issues the token.** Promise Tracker gives each coordinator a personal submission token, scoped to their meter ids. The coordinator pastes it into our dashboard.
- **Approve, then Send.** Approval is unchanged (§8c). An approved article gets a separate **Send to Promise Tracker** button. Our `site/` publishing stays as it is.
- **Only the assigned coordinator can send.** A send needs **all** of the following:
  1. the clicking user has an **active** assignment (primary, or temporary within its window) to the article's meter. Superadmins are **not** exempt;
  2. the user has their **own** valid token, and `whoami` lists that meter;
  3. the article is `approved`, and the deterministic QA (§3.6) passes on its current text;
  4. `submissions.enabled` and `publish.enabled` are both true. During shadow mode the button shows why it's disabled, and the server refuses the send too;
  5. the backend accepts it (its own scope check).
  Otherwise the send is refused and the refusal goes in the audit log. Another meter's article stays a 404, as today.

**Flow:**
1. **Profile → Promise Tracker token:** the user pastes the token. We call `whoami` and store the token **encrypted** (Fernet, key `PM_TOKEN_KEY`, outside the repo) with its token id, last 4 characters, meter ids and expiry. One active token per user. Replace and Remove are audited. The token is never shown again and never logged.
2. **Draft page** (approved articles): **Send to Promise Tracker**, with confirmation. We POST with `Idempotency-Key = article_id`, so a retry or double click can never create two submissions.
3. **Daily run:** a new `submissions_poll` stage after `review_pull` checks open submissions (1 request per second) and records `pending_review`, `published`, `rejected` or `withdrawn`, plus the reviewer note. The coordinator's digest lists the changes.
4. **Self-update suppression (rule 1):** when the Monitor sees a published update whose `submission_id` (or `update_id`) matches one of our submissions, the Classifier marks the event `suppressed_own_submission` and never drafts from it.

**Data:**
- **`pt_tokens`:** `id`, `user_id`, `token_ciphertext`, `token_id`, `last4`, `meter_ids` (from `whoami`), `expires_at`, `created_at`, `revoked_at`.
- **`pt_submissions`:**
  - `article_id` (UNIQUE), `meter_id`, `promise_id`;
  - `submitted_by` (user), `token_id`, `payload_sha256`;
  - `remote_id`, `remote_state`, `remote_update_id`, `reviewer_note`;
  - `submitted_at`, `last_checked_at`, `error`.
- `articles.state` is unchanged. Submission state lives in `pt_submissions`.

**Code:**
- `api/submissions.py` (`SubmissionClient`) is the only code that **writes** to admin.promisetracker.ng. It uses the same politeness and retry rules as §5.3, except that `POST` is retried **only** when the request has an idempotency key.
- `submissions.py` holds the domain logic: the authorisation checks above, payload building (`body_html` rendered with markdown html disabled) and polling.
- Config: `submissions: {enabled, base_url, poll_max_per_run}` in `config/settings.yaml`.
- Tests use a fake backend served through `httpx.MockTransport`, never the network.

**Until the backend exists:** P12 can be built and tested end to end against the fake, with `submissions.enabled: false`. Only the exit criterion "one live submission" waits for the backend.

*As built (2026-11-19):*
- **Self-update matching** doesn't depend on a new public API field. A new tracker update is ours if its id equals a submission's `remote_update_id` (from polling) **or** its text is within `classifier.own_submission_max_edit_ratio` (0.3) of the plain text we sent (`pt_submissions.body_text`). The text match covers an update published between two polls. It's stored as change type `own_submission` (logged, never an article), under the new_evidence dedupe key so it can't come back as evidence. `submission_id` on public updates is now optional in the contract.
- **Data:** `pt_tokens.remote_token_id` (the backend's id) and `holder_name`; `pt_submissions.token_row_id` (FK `pt_tokens`) and `body_text`. A submission row is written only after the backend accepts it. Refusals and failures are audit rows (`pt_submission.refused`, `pt_submission.failed`); the route commits them.
- **Daily run:** `submissions_poll` runs after `review_pull`. It checks up to `submissions.poll_max_per_run` open submissions, oldest-checked first, with the sender's token. A replaced or expired token leaves the status unknown and is noted for the admin. Decisions go to the meter's digest as `SENT` lines.
- **Dashboard:** "Promise Tracker token" in the sidebar (`/account/promise-tracker`): save (checked with `whoami`), replace, remove, and "Sent by you". The approved-article page shows **Send to Promise Tracker** or the list of reasons it can't be sent yet.
- **Migration:** 0012. **Config:** `submissions.{enabled, base_url (https only), timeout_seconds, poll_max_per_run}`, `classifier.own_submission_max_edit_ratio`, env `PM_TOKEN_KEY`.

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
| R-E5 | Editorial | The News Scout invents or misdates a URL, or uses a partisan source | Allowlist enforced in code; full dates only; **URL must appear in the raw search-tool results** (grounding); audit of `search_queries`; Verifier fetch, date check and relevance; coordinator approval |
| R-T10 | Technical/Ethical | About half the outlets block our honest User-Agent | Never spoof a browser. Confirm blocked URLs from raw search results and label them `verified_partial` so the coordinator sees it |
| R-T11 | Security | Fetching untrusted URLs (SSRF) | SafeFetcher: http(s) only, a public-IP check on every redirect hop, byte, time and redirect caps |
| R-E6 | Editorial | One story produces several near-duplicate articles across promises | Each URL is accepted for one promise only (highest relevance; earlier runs win) |
| R-T12 | Security | Formula injection through article text in Sheets; a leaked service-account key | RAW writes only; the key lives outside the repo (`~/.config/promise-monitor`, mode 600, `*.service-account.json` git-ignored); the account has no project roles and can reach only the sheets shared with it |
| R-S1 | Security | Dashboard exposure or account takeover would let someone steer a meter's news search | Local-only by default, and the server refuses public binding without HTTPS; scrypt hashes; lockout; CSRF; strict CSP; least privilege (coordinators see only assigned meters; outlets are superadmin-only); full audit log |
| R-E7 | Editorial | Per-meter search terms or outlets bias coverage | Terms only steer searching; every result still passes the global code gate (allowlist + meter extras, grounding, dates, relevance) and coordinator approval; outlet additions are superadmin-only and audited |
| R-S2 | Security | A leaked submission token lets someone file updates on a meter | Scoped by the backend to one person, their meters and `submissions:write`; expires and can be revoked; encrypted at rest; never logged; our dashboard also requires an active assignment; every send is audited |
| R-E8 | Editorial | Our own submitted update comes back as "new evidence" and is reported again | `submission_id` on published updates; the Classifier suppresses matches (§8d) |
| R-T8 | Cost | "Free tools only" | Postgres (local), Sheets, Gmail SMTP, cron, and GitHub Pages are all free; the LLM goes through the existing subscription |
