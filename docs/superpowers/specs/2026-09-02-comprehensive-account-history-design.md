# Comprehensive account history brief

**Date:** 2026-09-02
**Status:** Approved design
**Builds on:** `2026-09-02-hubspot-account-history-design.md` (the live count-based brief)

## Problem

The live brief (`hs_company_history`) surfaces *counts* — prior meeting count,
one deal, contact count, owner/last-activity. Aman asked for something more
comprehensive: a short **summary** of the prior relationship and **who we've
actually talked to**, so an AE walks into the call already briefed rather than
reading a tally.

## Goal

For a booked company that **already exists in HubSpot**, the second ("history")
thread comment renders:

```
📋 *Acme* — account history
Two calls since March on claims automation; stalled on pricing in June. Warm with ops, cold with finance.
• Talked to: Jane Doe (VP Ops), Mark Lee (CTO)
• Open deal: Acme - Intro Calls (Scheduled, $40k)
• Last touch: email, 2026-08-20
```

- **Summary** — a 2–3 sentence Claude narrative, grounded strictly in HubSpot
  records (meetings + notes + emails).
- **Talked to** — contacts who actually participated in prior meetings / email
  threads (meeting & email participants), name + title.
- **Open deal** and **Last touch** — compact facts.

Net-new companies stay silent and make zero extra calls (unchanged). The brief
is strictly additive: if the rich content isn't there, it degrades to the live
count block, and below that to silence.

## Non-goals / decisions locked in brainstorming

- Summary source: **everything** — meetings + notes + emails (richest option).
- "Talked to" source: **meeting/email participants only** (not all company
  contacts). If no participants are associated, the line is simply dropped.
- Format: **short narrative + facts line** (not a full labeled block).
- Delivery: **synchronous inside the existing second comment** (Approach A) —
  no background thread, no `chat.update`, no nightly precompute.

## Architecture (Approach A)

Extend the existing `hs_company_history(company_id)` helper and the
`_log_comment` renderer. No signature changes; no new call sites. All new work
runs only on the returning-account path, in the non-blocking second comment,
never in the main booking confirmation.

### Content gathering (new sub-reads in `hs_company_history`)

Each is wrapped like the existing sub-reads — a failure drops that source only,
never raises into the booking flow.

- **Meetings** — `POST /crm/v3/objects/meetings/search`, filter
  `associations.company EQ company_id`, properties `hs_meeting_title`,
  `hs_meeting_start_time`, `hs_meeting_body`, `hs_meeting_outcome`, sort
  `hs_meeting_start_time` DESC, `limit 8`.
- **Notes** — `POST /crm/v3/objects/notes/search`, filter
  `associations.company EQ company_id`, properties `hs_note_body`,
  `hs_timestamp`, sort `hs_timestamp` DESC, `limit 10`.
- **Emails** — `POST /crm/v3/objects/emails/search`, filter
  `associations.company EQ company_id`, properties `hs_email_subject`,
  `hs_email_text`, `hs_timestamp`, sort `hs_timestamp` DESC, `limit 10`.

### Participants (new)

HubSpot search does not return associations, so after the meeting + email
searches:

1. Two `POST /crm/v4/associations/{obj}/contacts/batch/read` calls
   (`obj` = `meetings`, then `emails`) over the ids already pulled — bounded by
   the same limits.
2. Collect the distinct associated contact ids.
3. One `POST /crm/v3/objects/contacts/batch/read` for properties `firstname`,
   `lastname`, `jobtitle`.

Result: `participants = [{'name': str, 'title': str|None}]`, de-duplicated and
capped at 6 for display. Any failure in this chain degrades to `[]`.

### Summary generation (new)

`_summarize_account(meetings, notes, emails) -> str | None`:

- Assemble a compact prompt: meeting titles + bodies, note bodies, email
  subjects + snippets. Clip each body/text to ~500 chars; hard-cap the whole
  assembled input (e.g. ~6000 chars) so a chatty account cannot blow the
  context window.
- Call the module-level `client` (`anthropic.Anthropic`, already initialized at
  `meeting_bot.py:109`) with model `claude-haiku-4-5-20251001`,
  `max_tokens≈180`.
- System instruction: "You brief an account executive before a sales call. In
  2–3 sentences, summarize the prior relationship strictly from the provided
  HubSpot records — meetings, notes, emails. State what was discussed and the
  current status. Do not invent facts; if the records are thin, say so
  briefly."
- Return the stripped text. Return `None` on empty input or any API error
  (wrapped) — never raises.

### `last_touch` (new, computed locally — no Claude)

The most recent `(type, date)` across the gathered meetings / notes / emails,
where `type` ∈ {`meeting`, `note`, `email`}. If no content was gathered, fall
back to `{'type': 'activity', 'date': hs_last_activity_date}` (the value the
live helper already reads). `None` if neither is available.

### Return shape

`hs_company_history` keeps all current keys (`meetings_count`,
`last_meeting_date`, `deal`, `contacts_count`, `owner_name`,
`last_activity_date`) and adds:

- `summary: str | None`
- `participants: list[{'name': str, 'title': str|None}]`
- `last_touch: {'type': str, 'date': str} | None`

### Rendering in `_log_comment`

When `history['summary']` is present, render the comprehensive format:

```
📋 *<Company>* — account history
<summary>
• Talked to: Name (Title), Name (Title)      # dropped if participants empty
• Open deal: <name> (<stage>[, $<amount>])    # dropped if no deal; 'Deal' if not open
• Last touch: <type>, <date>                  # dropped if last_touch None
```

Only lines with data render. Titles render as `Name (Title)`; when a title is
missing, just `Name`. The Segment/Size research lines and the "Couldn't find…"
missing-field line stay exactly as today.

**Degradation ladder:**

1. `summary` present → comprehensive block above.
2. `summary` is `None` but counts/deal/owner exist → fall back to the **live
   bullet block** (`We've spoken to *X* before: • N prior meetings (last: …) …`)
   exactly as shipped today.
3. Nothing → silent, as today.

So the comprehensive brief never regresses below current behavior.

## Error handling

Every new read, the two association batch reads, the contacts batch read, and
the Claude call are individually wrapped. Any one failing degrades to omitting
its piece; none can raise into the booking flow. Total added work on the
returning-account path: ~5 extra HubSpot calls + 1 haiku call, all bounded, on
the non-blocking second comment.

## Testing

Extend `tests/test_company_history.py`, mocking `requests` (as the suite does)
and the module `client`:

1. **Full content** — meetings/notes/emails + participant associations mocked,
   `_summarize_account` returns a narrative → narrative + `Talked to` + deal +
   last-touch block renders; participant names/titles correct.
2. **Claude fails** — content present but the Claude call raises → `summary`
   is `None` → renders the live bullet block (not the comprehensive one).
3. **`last_touch` selection** — most-recent source across meetings/notes/emails
   is chosen; falls back to `hs_last_activity_date` when content is empty.
4. **Net-new** — `co is None` path → history `None` → comment unchanged (still
   guarded by the existing test).
5. **Participants batch-read failure** — association read returns non-200 →
   `participants == []` → `Talked to` line dropped, rest of the block renders.

Claude is mocked in all tests — no live API calls.

## Explicitly out of scope (YAGNI)

- Background/async delivery, `chat.update` editing, nightly precompute.
- Summarizing anything beyond meetings/notes/emails (calls, tasks, tickets).
- Caching summaries between bookings.
- Listing all company contacts as a participant fallback (participants only, per
  brainstorming).
