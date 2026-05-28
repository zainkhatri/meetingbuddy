# Zoom recordings → HubSpot deals

**Date:** 2026-05-28
**Status:** Design approved, pre-implementation
**Owner:** Zain

## Problem

Close out the standing task "add Zoom meetings to deals." When a Zoom call with a
cloud recording finishes, the recording, transcript, and a summary should land on the
matching HubSpot **deal** automatically, so AEs/BDRs don't hand-copy call notes.

Shariq created a Zoom **General OAuth** app and shared the Client ID / Client Secret.
Cloud recording + audio transcript are being enabled on the account.

## Goal

A polling job pulls newly-completed Zoom cloud recordings **account-wide**, matches each
to a HubSpot deal, and writes a HubSpot **Note** onto the deal (and the meeting card)
containing:

- A **Claude-generated summary** (attendees, topics, next steps, objections/sentiment).
- The Zoom **recording URL** and **transcript URL**.
- The **full transcript text** (truncated with a link if it exceeds a safe note size).

No automatic deal-stage or field changes — logging only.

## Non-goals

- No webhook / real-time path (polling is sufficient).
- No deal-stage automation.
- No Slack notification for unmatched calls (log only — see below).
- No backfill of historical recordings in v1 (only recordings completed after go-live).

## Architecture

Runs as a **background thread inside the existing `meeting_bot.py` process on Railway**,
on a ~30-minute loop. Precedent: `sheet_reconcile` already runs periodically inside the
bot process (commit `b1864dc`). The bot already holds `HS_API_KEY`, `ANTHROPIC_API_KEY`,
and Slack tokens; we add `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` to its env.

### Modules

1. **`zoom_auth.py`** — OAuth token manager.
   - Exchanges the auth code (one-time) and refreshes access tokens against Zoom.
   - Handles Zoom's **rotating refresh tokens**: every refresh returns a *new* refresh
     token and invalidates the old one, so the new one MUST be persisted each cycle.
   - Reads/writes the refresh token from the durable state store (Railway volume).

2. **`zoom_sync.py`** — the poll loop.
   - `list_recent_recordings()` → Zoom `GET /v2/accounts/me/recordings` (admin scope),
     `from`/`to` covering the last N hours.
   - For each recording UUID not already processed:
     - Fetch transcript (`.vtt` / `.txt` from the recording's transcript file).
     - Match to a deal (see Matching).
     - On match: build Claude summary, write the Note, associate to deal + meeting,
       record UUID as processed.
     - On no/ambiguous match: log it and record UUID as processed (so it isn't retried
       forever). Do NOT guess a deal.

3. **`authorize_zoom.py`** — one-time local helper.
   - Runs the authorization-code flow against `http://localhost:3000/zoom/callback`.
   - Run once by an **account admin** Zoom login so the resulting token has account-wide
     recording scope. Prints/stores the refresh token for the durable store.

### State store (Railway persistent volume)

A small persistent volume mounted on the bot service holds `zoom_state.json`:

```json
{
  "refresh_token": "<rotating>",
  "processed_recording_uuids": ["..."]
}
```

- Survives restarts/deploys (Railway's default FS is ephemeral, hence the volume).
- Keeps the Zoom secret **out of the CRM** (rejected HubSpot-as-store for hygiene).
- `processed_recording_uuids` gives idempotency — same pattern as
  `scheduled_deal_sync.py`'s `processed_meeting_ids`.

## Matching (hybrid, in priority order)

1. **Exact — Zoom meeting ID in a HubSpot meeting engagement.** GCal-synced meetings carry
   the Zoom join URL in their `location`/URL field; the Zoom meeting ID is embedded there.
   Match → use that meeting's associated deal.
2. **Host + time fallback.** Host email + start-time window (±10 min) → HubSpot meeting →
   its associated deal.
3. **Email fallback.** Participant emails (excluding `@furtherai.com`) → HubSpot contact →
   their single open deal.
4. **Unmatched / ambiguous** (zero matches, or a contact with multiple open deals) → write
   a line to the bot log with recording link + participants + reason, mark UUID processed,
   attach nothing. Never guess a deal.

## What lands on the deal

A HubSpot **Note** engagement, associated to both the deal and the matched meeting card:

- **Top:** Claude summary — attendees, topics discussed, next steps, objections/sentiment.
- **Links:** Zoom recording URL, transcript URL.
- **Body:** full transcript text. If it exceeds a safe note size (~60k chars), truncate and
  append a "full recording" link.

Claude call reuses the bot's existing `anthropic` client / `ANTHROPIC_API_KEY`.

## Scopes required

General OAuth (user-managed) app, authorized by an **account admin**, with admin recording
scopes:

- `cloud_recording:read:list_account_recordings:admin` (list account recordings)
- `cloud_recording:read:recording:admin` (read a recording + its transcript)
- `user:read:user:admin` / appropriate user-read scope to resolve host email if needed

(Exact scope strings to be confirmed against the app's scope picker during setup.)

## Error handling / reliability

- Per-recording try/except: a poison recording logs and the loop continues to the next.
- Token refresh failure logs clearly (likely expired/rotated-out refresh token → re-auth).
- Idempotent: a UUID is marked processed once handled (matched, unmatched, or errored past
  a retry budget) so the loop never wedges on one item.
- Consistent with the bot's "locked down, not best-effort" stance: layered recovery, the
  thread should never crash the bot process.

## One-time setup (coordination, not code)

1. Shariq adds the required scopes to the General OAuth app and sets redirect URI
   `http://localhost:3000/zoom/callback` (already done) — and enables cloud recording +
   audio transcript on the account.
2. The **admin authorization** must happen in a browser on the machine running
   `authorize_zoom.py` (localhost redirect). Simplest path: Shariq temporarily grants Zain
   the Zoom admin role so Zain can self-authorize with account scopes. Alternatives:
   screen-share approval, or Shariq runs the helper himself.
3. Store the resulting refresh token into the Railway volume `zoom_state.json`.
4. Add `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` to the bot's Railway env.
5. **Rotate the Client Secret** afterward — it was shared in plaintext over Slack and chat.

## Open items to confirm during implementation

- Exact Zoom scope strings from the app's scope picker.
- Whether GCal→HubSpot meetings reliably carry the Zoom meeting ID in `location` (verify on
  a real synced meeting before relying on match #1).
- Note size limit behavior in the HubSpot Notes API (truncation threshold).
