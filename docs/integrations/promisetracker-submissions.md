# Promise Tracker submissions API: contract for the backend team

**Status:** proposal, 2026-11-19. Nothing here exists on `admin.promisetracker.ng` yet as far as we know. Spec owner: PRD §8d (P12).

## Why

AdvoKC's tracker-agent pipeline drafts short, sourced updates on promises. A coordinator approves each one in the tracker-agent dashboard. Today a coordinator has to log in to the Promise Tracker admin and retype the update. This API lets the tracker-agent dashboard **submit an approved update into the Promise Tracker review queue** on the coordinator's behalf, using a token the backend issued to that coordinator. Nothing is published by this API. A Promise Tracker editor still reviews and publishes every submission in the admin, as they do today.

## What we need from the backend

1. **Per-coordinator submission tokens**, issued in the Promise Tracker admin.
2. **Three endpoints:** `whoami`, `create submission` and `get submission`.
3. **The published update id** in the submission status (`update_id`), so our Monitor doesn't report our own update as new evidence. (Optional extra: `submission_id` on public updates.)

## Tokens

- The Promise Tracker admin issues each token to **one person** and limits it to **one or more meter ids**. A coordinator with two meters gets one token covering both, or two tokens.
- **Format:** opaque bearer string, high entropy (≥ 32 random bytes), with a recognisable prefix (for example `ptsub_`) so leaked tokens can be found by secret scanners.
- **Scope:** `submissions:write` only. A submission token must not be able to publish, edit or delete anything, or read unpublished content beyond its own submissions.
- **Lifetime:** has an expiry (we suggest at most 90 days), and an admin can revoke it at any time.
- **Storage (backend side):** store only a hash of the token. Show the token once, when it's issued.
- **Sent as:** `Authorization: Bearer <token>`.

## Common rules

- **Base URL:** `https://admin.promisetracker.ng/api/v1/promise-tracker/submissions`. Change it if you prefer; it's one config value on our side.
- **Envelope:** the existing one, `{"success": bool, "data": ..., "message": str}`.
- **IDs:** meter and promise ids are the same UUIDs the public API already returns.
- **Timestamps:** ISO-8601 with an offset.
- **Rate:** we send at most 1 request per second, one at a time, with an identifying `User-Agent`. A `429` response with `Retry-After` is respected.
- **HTTPS only.**

| Status | Meaning |
|---|---|
| 401 | Token missing, unknown, expired or revoked |
| 403 | Token valid, but the meter isn't in its scope |
| 404 | Promise or submission not found, or not visible to this token |
| 409 | Idempotency key reused with a different payload |
| 422 | Payload failed validation. `data.errors` is a list of `{field, message}` |
| 429 | Slow down |

## `GET /whoami`

Validates a token when a coordinator saves it in our dashboard. The backend must not log the token itself.

```json
{
  "success": true,
  "data": {
    "token_id": "b1c2…",
    "holder_name": "Coordinator display name",
    "meter_ids": ["6f1e…", "a903…"],
    "scopes": ["submissions:write"],
    "expires_at": "2027-02-17T00:00:00+00:00"
  },
  "message": "ok"
}
```

## `POST /` (create a submission)

**Headers:** `Authorization: Bearer …`, plus `Idempotency-Key: <our article_id UUID>`.

```json
{
  "meter_id": "6f1e…",
  "promise_id": "0768fc43-0117-49a5-a113-8d8dfb011acc",
  "headline": "Lagos opens second phase of the Red Line rail",
  "body_markdown": "…60–100 words with [descriptive links](https://…)…",
  "body_html": "<p>…same text, rendered; only <p>, <a href>, <em>, <strong></p>",
  "sources": [
    {"url": "https://…", "title": "…", "publisher": "…", "published_date": "2026-11-12"}
  ],
  "external_ref": "tracker-agents:article:<article_id>",
  "approved_at": "2026-11-19T09:12:00+00:00"
}
```

**Backend behaviour:**

- **Checks:** reject with 403 if `meter_id` isn't in the token's scope. Reject with 422 if `promise_id` doesn't belong to `meter_id`.
- **Creates** an update on that promise with `is_published = "0"` (or your equivalent review state), attributed to the token holder.
- **Doesn't touch the rating:** the submission never changes the promise's `status` or `last_published_status`. Ratings stay a Promise Tracker editorial decision.
- **Idempotent:** the same `Idempotency-Key` with the same payload returns the original submission (200). The same key with a different payload returns 409.
- **Sanitises:** treat `body_html` as untrusted. Sanitise it or re-render it from `body_markdown`.

**Response (`201 Created`):**

```json
{
  "success": true,
  "data": {"submission_id": "c55d…", "state": "pending_review", "created_at": "…"},
  "message": "created"
}
```

## `GET /{submission_id}`

We check open submissions once a day (at most 1 request per second) so the coordinator can see what happened to theirs.

```json
{
  "success": true,
  "data": {
    "submission_id": "c55d…",
    "state": "pending_review | published | rejected | withdrawn",
    "update_id": "e0a1… or null (the promise update id once published)",
    "reviewer_note": "string or null",
    "updated_at": "…"
  },
  "message": "ok"
}
```

## Public API change (optional)

We recognise our own published update from `update_id` above, or from its text. If it's easy, also add `"submission_id": "c55d…"` to each `updates[]` entry on `/view/promise/{id}` that came from a submission. It would make the match exact.

## Open questions for the backend team

1. Does the admin already have a draft/review state for updates that this can reuse?
2. The house-style closing line links to `promisetracker.ng/blog/{promise_id}`. On the promise's own page, that link points back to the page it's on. Should we leave the closing line out of the submission?
3. What's the maximum headline length, and is a headline field even wanted on an update?
4. Should we send `update_type` (and if so, which values exist)?
5. Who can issue tokens in the admin, and is there an audit trail for issuing and revoking them?
