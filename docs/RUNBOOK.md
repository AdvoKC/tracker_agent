# Runbook: Promise Tracker Monitor

How to operate the daily pipeline. The spec is `docs/PRD.md`; the rules for code changes are in `AGENTS.md`.

## What runs, and when

| What | When | How |
|---|---|---|
| Daily run (`promise-monitor run`) | 08:00 WAT. If the machine was off, it runs once at the next boot or login. | systemd user timer `promise-monitor.timer` → `promise-monitor.service` |
| Crash alert email | Only if the run process dies before it can report | `OnFailure=promise-monitor-alert.service` → `promise-monitor alert` |

The stages of a run, in order (each is isolated: one failing doesn't stop the rest):
1. Housekeeping (delete logs older than 90 days)
2. Heartbeat
3. Pull coordinator decisions from the sheet
4. Publish (**off in shadow mode**)
5. Discover meters
6. Monitor and classify
7. News Scout
8. Verify sources
9. Write drafts
10. Push new rows to the sheet
11. Email the digests and alerts

Only one writing command can run at a time, enforced by a Postgres advisory lock. If the timer fires while you're running something by hand, the run exits 0 with "lock held; skipping".

## Daily checks (2 minutes)

1. **Email.** Admin alerts arrive only when something needs you. Subjects:
   - `[promise-monitor] DEGRADED` or `FAILED`
   - `URGENT`: 2 failed runs in a row, no successful run for 36 hours, or the Claude login failed
   - `CRASH`: the process died before it could report
2. **Coordinators** get `[Promise Tracker] N item(s) to review` when their meter has new drafts, leads or stale items.
3. **Status at a glance:**
   ```bash
   systemctl --user list-timers promise-monitor.timer       # next/last run
   journalctl --user -u promise-monitor.service -n 50       # last run's console output
   uv run promise-monitor shadow-report --days 7            # summary + verdict
   ```

## Where to look

- **Runs:** table `runs` (status, stats JSON with `stage_errors` and `warnings`).
  `select started_at, status, stats->'stage_errors' from runs where trigger='cli:run' order by started_at desc limit 5;`
- **Logs:** `logs/YYYY-MM-DD.jsonl`, one JSON object per line, every line tagged with `run_id`. Filter a run with `grep <run_id> logs/2026-11-18.jsonl`.
- **LLM usage:** table `llm_calls` (agent, model, cost_usd, error).
- **Why something was or wasn't written:** `change_events` (score, score_reason, state), `news_items` (reject_reason), `article_groups` (state, notes), `article_group_sources` (verdict per source), `articles` (writer_input, qa_flags).

## Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| URGENT "Claude login failed" | The CLI OAuth session expired | Log in as the service user: run `claude`, type `/login`, then `/exit`. The next run picks it up. |
| FAILED "promisetracker.ng API unreachable" | The site or API is down, or there's no network | Nothing to do. The next run catches up from stored state. Check https://admin.promisetracker.ng if it repeats. |
| "anomaly guard tripped" on a meter | More than 30% of a meter's promises changed at once (bulk edit or data migration on the site) | Nothing was stored. Confirm with the content team that it was deliberate, then run `uv run promise-monitor monitor --meter <id>`. If the guard trips again, raise `monitor.anomaly_change_ratio` temporarily. |
| "skipped (… validation failed …)" | The API changed shape | Check the error in the logs. Refresh fixtures (`tests/fixtures/api/`), update `api/models.py`, add a test. |
| "rate limited by the tracker API" | Too many requests | It continues tomorrow. If persistent, raise `api.min_interval_seconds`. |
| "review sheet not configured / could not be opened" | `.env` is missing the Sheets settings, or the sheet isn't shared with the service account | Re-run `scripts/setup_google_sheets.sh`. |
| "N active meter(s) have no coordinator email" | The `coordinators` tab is incomplete | Fill in `email` for those meters. Until then, their digests go to the admin. |
| Many `needs_sources` leads | Sources block our reader and aren't in search results, or genuinely can't be verified | Expected for some outlets. Coordinators check the leads and either update Promise Tracker or `dismiss`. |
| CRASH email | Database down, disk full, or a bug | `journalctl --user -u promise-monitor.service -n 200`. Fix the cause, then run by hand (below). |
| No email at all | SMTP not configured, or the Gmail App Password was revoked | `uv run promise-monitor notify-test`. |

## Manual operations

```bash
uv run promise-monitor run                    # a full run now (respects the lock)
systemctl --user start promise-monitor.service   # same, via systemd (logs to journald)
uv run promise-monitor run --no-email         # without sending email
uv run promise-monitor monitor --meter <id>   # one meter
uv run promise-monitor review-sync            # sync the sheet now
systemctl --user stop promise-monitor.timer   # pause daily runs
systemctl --user start promise-monitor.timer  # resume
scripts/install_timer.sh                      # (re)install / update units after a code change
scripts/install_timer.sh --remove             # uninstall
```

Re-running is always safe. Snapshots, dedupe keys and group states make every stage idempotent. A change is never reported twice.

The timer only fires while you're logged in unless linger is enabled. Enable it once with `sudo loginctl enable-linger $USER`.

## Shadow mode → going live

Publishing is **off** (`publish.enabled: false` in `config/settings.yaml`) for the first 2 weeks. During that time:
- coordinators review and approve as normal;
- approved articles are **held**, and the sheet says "shadow mode: held";
- nothing reaches `site/`.

To go live:
1. `uv run promise-monitor shadow-report --days 14`. It must say `VERDICT: PASS`: 14 consecutive good days, 0 duplicate articles, 0 runs with stage errors. Also read the approval rate and the "needed attention" count.
2. Read a sample of approved drafts with the editorial lead.
3. Set `publish.enabled: true` and commit.
4. The next run publishes **all held approvals**, each dated by its publication time. To drop stale ones first, set their `decision` to `reject` in the sheet before the run.

## Admin dashboard (review happens here)

Coordinators review drafts in the **dashboard** (Review): edit, then Save & re-check, Approve or Reject, and Dismiss leads. The Google Sheet queue is no longer used (`review.channel: dashboard`), so **`scripts/setup_google_sheets.sh` is not needed**. Digests link to `review.dashboard_url`; update it when the dashboard moves to the VPS.

What a superadmin can do from the dashboard:
- **Overview:** health at a glance, plus "Run the pipeline now".
- **Meter page:** "Re-check promises now" and "Re-scan news now". **Draft page:** "Redraft with Claude". **Lead page:** "Re-verify sources". These run as **Jobs**; open one to see its output. "busy" means another run held the lock, so try again later.
- **Settings:** article threshold, News Scout on/off and calls per run, relevance minimums, stale days, anomaly guard, the outlet allowlist, and **shadow mode** (Publishing on/off). Every change is audited and applies from the next run.
- **Users → a user:** tick the meters they coordinate, grant temporary cover, change role, reset password.
- **Updates / Searches / Sources / Runs / LLM usage / Audit:** everything the agents did and why.


```bash
uv run promise-monitor create-superadmin   # once: your account (asks for a 12+ character password)
uv run promise-monitor dashboard           # then open http://localhost:8800
```
- Set `PM_DASHBOARD_SECRET` in `.env` first. Without it, everyone is signed out whenever the dashboard restarts.
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- **To keep it running in the background (optional):** copy `scripts/systemd/promise-monitor-dashboard.service` to `~/.config/systemd/user/`, replacing `@REPO@` with the repo path. Then run `systemctl --user enable --now promise-monitor-dashboard`.
- **Add a coordinator:** Users → Add user. Share the one-time password privately; they must change it at first sign-in. Then open the meter and use "Make primary".
- **Leave cover:** open the meter → "Grant temporary access" → choose the coordinator, the From/Until times (WAT) and a reason. Access starts and ends by itself, and the cover also receives that meter's digest. "Revoke" ends it early.
- **Search context:** set it per meter: names and aliases, search terms, exclude terms, and (superadmin only) extra local outlets. It takes effect from the next run. Every change appears in the Audit log.
- **Someone locked out:** 5 wrong passwords lock that email for 15 minutes. A superadmin can "Reset password" to issue a new one-time password.
- **On the VPS (later):** run it behind an HTTPS reverse proxy (Caddy or nginx), set `PM_DASHBOARD_HTTPS=true`, and start it with `--host` set to the proxy-facing address. The CLI refuses a non-local address otherwise.

## Sending updates to Promise Tracker (P12)

Coordinators can send an **approved** article to Promise Tracker's review queue from its draft page (**Send to Promise Tracker**). Promise Tracker's editors still decide whether to publish it. Off until the backend endpoints exist; see `docs/integrations/promisetracker-submissions.md`.

- **One-time setup:** generate `PM_TOKEN_KEY` (command in `.env.example`) and put it in `.env`. Keep a copy with your secrets backup: if it's lost or changed, saved tokens can't be read and coordinators must paste them again.
- **Turn it on:** `submissions.enabled: true` in `config/settings.yaml`. Sending also needs `publish.enabled: true`, so nothing is sent in shadow mode.
- **Each coordinator:** gets a token issued in the Promise Tracker admin, then pastes it under **Promise Tracker token** in the sidebar. The page lists the meters it covers. They can send only for meters they're **assigned to here** that the token **also** covers. Superadmins are not exempt.
- **Why can't I send?** The draft page lists every reason: not approved, already sent, switched off, shadow mode, not assigned, no token or token doesn't cover the meter, or the text fails QA.
- **Status:** the daily run checks open submissions and adds `SENT` lines to the coordinator's digest (published or rejected, with the reviewer's note). The "Sent by you" list is on the token page.
- **Lost or leaked token:** revoke it in the Promise Tracker admin, then **Remove token** here. Every send, refusal and failure is in the Audit log (`pt_submission.*`).

## Backups

The database holds the only record of what has already been reported.
- Back it up at least weekly:
  `pg_dump -Fc "postgresql://promise_monitor:…@localhost:5432/promise_monitor" > ~/backups/promise_monitor-$(date +%F).dump`
- Restore with `pg_restore -d <empty db> <file>`. If the database is lost entirely, rebuilding it starts with a silent baseline. No duplicates, but changes made since the last backup are not reported.
