# Conference back-and-forth resolution — Design

**Goal:** When a `#conference-meetings` booking names no identifiable conference, the bot should stop dead-ending. Instead it asks in-thread which conference it is, and when a human replies with the name, the bot resolves it and re-stamps the meeting's `conference_source` in HubSpot.

**Today's behaviour:** `_maybe_unsure_reply` (`meeting_bot.py:924`) posts a flat `"Not sure what conference that is"` and nothing consumes the human's answer — thread replies fall into `handle_message`, parse as non-bookings, and get dropped.

## Architecture

Stateless. No `thread_ts → meeting_id` map is stored, because the meeting is already stamped with `booked_at` = the Slack post timestamp in ms (`meeting_bot.py:1017`, `:1129`). A thread reply's `thread_ts` **is** the parent booking's `ts`, so the meeting is found by a single `booked_at EQ` search — the exact query already used for replay dedup (`meeting_bot.py:1350`). This survives the 30-minute periodic restart for free, with no re-parse of the parent message through Claude.

## Components

### 1. Reword the unsure reply — `_maybe_unsure_reply` (`meeting_bot.py:924`)

Change the text from `"Not sure what conference that is"` to:

> `Not sure what conference that is — reply with the name and I'll tag it.`

Firing conditions unchanged: `channel == CONFERENCE_MEETINGS_CHANNEL and conf in (None, 'other') and say and ts`. Live bookings only (sweep/replay pass a silent `say`).

### 2. Route thread replies — `handle_message` (`meeting_bot.py:816`)

A booking post is top-level (no `thread_ts`). A human's answer is a thread reply (`thread_ts` present and `!= ts`). Early in `handle_message`, after the subtype handling that sets `text`/`ts`, add:

```python
thread_ts = event.get('thread_ts')
if thread_ts and thread_ts != ts and event.get('channel') == CONFERENCE_MEETINGS_CHANNEL:
    _handle_conference_reply(thread_ts, text, say)
    return
```

Placed before `_claim_ts(ts)` and the parse path so a reply is never treated as a new booking. `message_changed` edits of a reply re-run harmlessly (the handler is idempotent).

### 3. Resolve + re-stamp — `_handle_conference_reply(thread_ts, text, say)` (new)

1. `booked_ms = int(float(thread_ts) * 1000)`.
2. Search HubSpot meetings for `booked_at EQ str(booked_ms)`, requesting `conference_source`, `hs_meeting_title`, `hs_meeting_start_time`. Take the first result.
3. **Guards (silent no-op, no reply):**
   - No meeting found → return. (Reply to a non-booking, or a race before the meeting exists.)
   - `conference_source` already a real value (not `None`, not `'other'`) → return. This is what keeps ordinary post-resolution thread chatter from being reprocessed.
4. Resolve the reply `text` to a conference value:
   - First `detect_conference_from_title(text)` — the existing regex map (`_CONF_RULES`) covers WSIA / TMPAA / RIMS / Target Markets / etc.
   - Else `resolve_or_create_conference(text, meeting_date)` for a genuinely new event (derives year from the meeting's start time when the reply omits it).
5. **Resolved** → `PATCH` `conference_source` on the meeting. Reply `✓ tagged {label}` in-thread (`thread_ts=thread_ts`). `label` is the human-readable conference name (from `resolve_or_create_conference`'s `label`, or the resolved value for regex hits).
6. **Unresolved** (both paths returned nothing) → reply `Still couldn't identify "{text}" — set it in HubSpot manually.`

All HubSpot calls wrapped in try/except with `print(..., flush=True)` on failure, matching the module's existing error style; a failure never raises out of the handler.

## Data flow

```
Booking post (no thread_ts) → handle_message → _process_booking
    → conference_source unresolved (None/'other')
    → _maybe_unsure_reply posts the question in-thread
Human reply (thread_ts = booking ts) → handle_message
    → routed to _handle_conference_reply
    → booked_at EQ lookup → meeting
    → resolve reply text → conference value
    → PATCH meeting.conference_source → "✓ tagged X"
Next sheet_reconcile_loop pass → propagates conference_source to Ellen's sheet
```

**Sheet propagation:** `conference_source` on the meeting is re-stamped in HubSpot only. The existing `sheet_reconcile_loop` (HubSpot → Ellen's Full Meeting Tracker) re-scans meetings and upserts, carrying the new conference through. **To verify during implementation:** confirm the reconcile actually maps `conference_source`; if it does not, add a direct `_push_to_ellen_sheet` call from the reply handler (pulling company/contact off the meeting's associations).

## Error handling

- HubSpot search / PATCH failure → log, no reply (avoid noisy false confirmations), meeting stays unchanged; the human can retry by replying again.
- `float(thread_ts)` is always valid for a real Slack ts; wrap defensively anyway and no-op on failure.
- Reply arrives after the meeting was already tagged (e.g. two people answer) → second reply hits the "already real" guard and silently no-ops.

## Testing

Pure/unit-testable seams, mirroring `tests/test_conference_autocreate.py` (monkeypatch HubSpot + say):

- **routing:** a `thread_ts`/`ts` mismatch in the conference channel calls `_handle_conference_reply`; a top-level post (no `thread_ts`) does not; a thread reply in a *non*-conference channel does not.
- **resolve to existing:** meeting found with `conference_source='other'`, reply `"WSIA"` → PATCH called with `wsia_uw_summit`, reply contains `✓`.
- **already tagged guard:** meeting found with `conference_source='wsia_uw_summit'` → no PATCH, no reply.
- **no meeting guard:** `booked_at` search returns empty → no PATCH, no reply.
- **unresolved:** reply `"asdf"` with no regex hit and `resolve_or_create` returning `None` → reply contains `Still couldn't identify`.

## Out of scope (YAGNI)

- No auth on who may answer — anyone in the thread naming the conference is accepted.
- No confirm-before-write — the handler only overrides `None`/`other`, never an already-resolved conference.
- No in-memory pending-question map — the `booked_at` stamp is the link; nothing to persist or lose on restart.
