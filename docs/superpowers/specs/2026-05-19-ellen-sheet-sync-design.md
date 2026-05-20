# Ellen's BDR Meeting Tracker — Sheet Sync

**Date:** 2026-05-19
**Owner:** Zain
**Stakeholders:** Ellen (consumer), Aman (priority), Nia

## Problem

Ellen tracks all conference meetings in a Google Sheet ("BDR Meeting Tracker" tab of [sheet `1dpp57GTua_cirRynJOQKtkjFzc4tJihoSjIwK9PWuqs`](https://docs.google.com/spreadsheets/d/1dpp57GTua_cirRynJOQKtkjFzc4tJihoSjIwK9PWuqs)). BDRs currently maintain it by hand. Meeting Bot already captures every conference meeting from Slack into HubSpot — duplicating that data into Ellen's sheet automatically removes manual work and gives Ellen/Aman real-time visibility into "meetings booked" as a conference KPI.

Out of scope: post-conference deal-pipeline ROI (new deals, POCs, customers + $). That's a separate spec.

## Goals

- Every HubSpot meeting created/updated by the bot for a detected conference shows up as a row in Ellen's sheet within seconds.
- Pre-existing rows for the same (company, date) are updated in place, not duplicated.
- Sheet writes never block or break the HubSpot path — if Sheets is down, bot keeps working.
- A daily reconciler catches anything missed (transient failures, manual HubSpot edits).

## Architecture

Hybrid: inline write + daily reconciler.

```
Slack message
   │
   ▼
meeting_bot.py
   │  ── creates/updates HubSpot meeting (existing behavior)
   │
   ├──► sheet_sync.upsert_meeting_row(payload)   [try/except, non-blocking]
   │       │
   │       ▼
   │     Google Sheets API (gspread)
   │
   └──► Slack reply: "✓ logged to HubSpot + Ellen's sheet"

Daily cron (Railway, 6 AM ET):
   scripts/sheet_reconcile.py
     - Pulls HubSpot meetings from last 7d with conference detected
     - upsert_meeting_row() for each (safety net)
```

## Components

### `sheet_sync.py` (new)

Single module. Public surface:

```python
def upsert_meeting_row(payload: dict) -> dict:
    """Upsert a row in Ellen's sheet.
    Match key: (Prospect Company, Meeting Date).
    Returns {'action': 'inserted'|'updated'|'skipped', 'row': int}.
    Never raises - caller decides what to do on error via return.
    """
```

Internals:
- `gspread` client built from `GOOGLE_SERVICE_ACCOUNT_JSON` env var (JSON string).
- Sheet handle cached at module level; `ELLEN_SHEET_ID` + `ELLEN_TAB_NAME` env vars.
- Header row read once on first call to map column names → indices (resilient to column reorder).
- Match: scan column `Prospect Company` + `Meeting Date` (case-insensitive, trimmed). Skip rows where `Meeting Sourced By` was manually set if our payload disagrees? **No** — bot is source of truth; overwrite. But never blank out a cell that has a value when our payload's value is empty (preserves BDR-added Notes, AE, Location, Follow-Up).
- Append uses `worksheet.append_row` with `value_input_option='USER_ENTERED'`.
- All writes wrapped in retry-with-backoff (3 tries) for 429/5xx.

### `meeting_bot.py` (modified)

In `_process_booking`, after successful `hs_create_meeting` or update path:

```python
if parsed.get('conference_source'):
    try:
        result = sheet_sync.upsert_meeting_row(build_sheet_payload(...))
        sheet_status = f" + sheet ({result['action']})"
    except Exception:
        logging.exception("sheet upsert failed")
        sheet_status = ""  # silent fail in Slack reply
```

New helper `build_sheet_payload(parsed, contact, company, meeting_props, owner_name)` translates HubSpot fields → sheet column dict.

### `scripts/sheet_reconcile.py` (new)

Standalone script. Run via Railway cron `0 6 * * *`.
- Searches HubSpot meetings where `hs_meeting_start_time` is within last 7d and `conference_source` (or detected from title) is non-empty.
- For each, builds the same payload and calls `upsert_meeting_row`.
- Logs per-row action; exits 0 always (cron resilience).

## Column Mapping

| Sheet Column | Bot Source | Notes |
|---|---|---|
| Conference | `detect_conference_from_title/date` | Already implemented |
| Meeting Sourced By | Slack poster → display name | Whoever dropped the message: Zain, Dani, Jacob, etc. |
| Account Executive | _blank_ | BDRs fill manually |
| FurtherAI Rep in Meeting | _blank_ | BDRs fill manually (often differs from sourced by) |
| Meeting Date | `hs_meeting_start_time` → `M/D` | ET timezone |
| Meeting Time | `hs_meeting_start_time` → `H:MM AM/PM ET` | ET timezone |
| Meeting Location | _blank_ | Not tracked by bot |
| Prospect Company | associated company `name` | Match key |
| Prospect Name | contact `firstname` + " " + `lastname` | |
| Prospect Title | contact `jobtitle` | |
| Prospect Email | contact `email` | |
| Meeting Status | `hs_meeting_outcome` mapped: SCHEDULED→"Scheduled", COMPLETED→"Completed", CANCELED→"CANCELLED", NO_SHOW→"No Show" | |
| Notes | _blank_ | BDRs fill manually |
| Follow-Up Demo Scheduled? | _blank_ | BDRs fill manually |

Preservation rule: when updating an existing row, never overwrite a non-empty cell with an empty value. BDR-added context (Notes, AE, Location, Follow-Up) survives.

## Auth & Config

New env vars (Railway):
- `GOOGLE_SERVICE_ACCOUNT_JSON` — raw JSON of a service account with Sheets API enabled
- `ELLEN_SHEET_ID` — `1dpp57GTua_cirRynJOQKtkjFzc4tJihoSjIwK9PWuqs`
- `ELLEN_TAB_NAME` — `Full Meeting Tracker`

Setup steps:
1. Create GCP service account, enable Sheets API, generate JSON key.
2. Share the sheet with the service account email (Editor).
3. Add env vars to Railway.

`requirements.txt` adds: `gspread>=6.0`, `google-auth>=2.0`.

## Error Handling

- Missing env vars on startup: log warning, `upsert_meeting_row` becomes a no-op that returns `{'action': 'skipped', 'reason': 'unconfigured'}`. Bot continues normally.
- Sheets API error: 3-retry backoff, then log + return `{'action': 'error'}`. Caller swallows.
- Header changes in sheet: refetched on next module load (process restart). Column name mismatch → log "unknown column X" and skip that field.
- Conference not detected: skip sheet entirely (Ellen's sheet is conference-only).

## Testing

- Unit tests for `build_sheet_payload`: HubSpot dict → sheet column dict.
- Unit tests for match logic: same company different date → insert; same (company, date) → update; case-insensitive company match.
- Manual end-to-end: post a fake booking in Slack `#meeting-bot-test`, verify row appears in a test tab of the sheet.
- Reconciler: dry-run mode (`--dry-run` flag) prints actions without writing.

## Rollout

1. Build + test against a duplicate of Ellen's sheet (new test tab).
2. Switch to live tab; backfill last 30d via `sheet_reconcile.py --since 30d`.
3. Announce to Ellen + Aman in Slack.
4. Monitor for 1 week before enabling the daily cron.

## Future Work (separate spec)

- Aman's pipeline ROI: total new deals, POCs, customers + $ values, sourced from HubSpot Deals filtered by conference attribution. Will live in a separate tab/sheet and is driven by deal stage transitions, not meeting creation.
