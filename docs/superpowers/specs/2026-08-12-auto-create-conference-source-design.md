# Auto-create HubSpot event sources for new conferences

**Date:** 2026-08-12
**Status:** Design — awaiting approval

## Problem

A conference booking announced in `#conference-meetings` for an event the bot
has never heard of ("Event Source: BTC 2026") gets filed into the catch-all
`other` bucket in HubSpot's `conference_source` property. Marketing's
event-sourced reporting has no BTC bucket receiving it, so the meeting looks
unallocated.

Confirmed on the live record: meeting `390614433491` (The Best MGA, Dani) is
stamped `conference_source: 'other'`, title `FurtherAI + The Best MGA [other]`.
The `conference_source` dropdown has 18 options; none is BTC.

### Root cause

Three layers only recognize a fixed set of conferences, and there is no path
for a new one to create itself:

1. `_BOOKING_ITEM` schema (`meeting_bot.py:176`) — `conference_source` is a
   **closed enum**. Claude physically cannot emit "btc"; it can only pick a
   known slug or `other`. So a new event is invisible past the parser.
2. `_CONF_RULES` regex (`:289`) and `_CONF_DATE_WINDOWS` (`:323`) — fallbacks,
   also fixed lists. No BTC pattern/window, no rescue.
3. HubSpot `conference_source` dropdown — enumeration property; only accepts
   values already defined as options. Even if the bot emitted `btc`, the PATCH
   would 400 until the option exists.

## Goal

When a booking names a conference with no existing HubSpot bucket, the bot
**creates the option in HubSpot automatically** and files the meeting to it —
no manual pre-creation. Without turning the report into a junk generator.

## Design

### 1. Surface the raw event name (schema)

Add a nullable field to `_BOOKING_ITEM`:

```
'conference_name_raw': {'type': ['string', 'null']}
```

Prompt guidance (in `PARSE_PROMPT`, conference section): fill
`conference_name_raw` with the **literal event name as written** — but ONLY
from an explicit `Event Source:` / `Source:` line or a conference header.
Do not infer from company name or stray words. Null otherwise.

The existing closed `conference_source` enum stays — for known events Claude
still maps to the canonical slug (BTC will come back as `other`, which we treat
as "unresolved, check raw").

### 2. Normalize (pure function)

```
slugify_conference(raw) -> (value, label)
```

- `label`: raw, trimmed, trailing year token stripped ("BTC 2026" -> "BTC").
- `value`: label lowercased, non-alphanumeric -> `_`, collapse repeats, strip
  edge `_` ("BTC" -> "btc"). Cap length at 50.
- Returns `None` if the result is empty or all-digits (guards junk).

### 3. Resolve-or-create (the core)

```
resolve_or_create_conference(raw) -> value | None
```

1. `slugify_conference(raw)` -> candidate `(value, label)`. None -> return None.
2. Fetch the dropdown options (in-process cache; refresh after any create).
3. **Dedup:** normalize every existing option's `value` AND `label` the same
   way. If candidate value == an existing value, or candidate normalized-label
   == an existing normalized-label -> return the **existing** value. No create.
   (This collapses "WSIA" spelled a new way back onto `wsia_uw_summit`.)
4. Otherwise **create**: under a lock, re-fetch options (race guard), append
   `{label, value, hidden: false, displayOrder: -1}`, `PATCH` the full options
   array back to `/crm/v3/properties/meetings/conference_source` (HubSpot
   replaces the whole list — existing options, incl. hidden ones, preserved).
5. Post a Slack notice (see §5). Return the new value.

Concurrency: the live handler and the `live_sweep` thread can process bookings
simultaneously. Create runs under a dedicated lock, and re-fetches options
inside the lock, so two new-conference bookings can't double-create or clobber
each other's options.

### 4. Wire into the flow

In `_process_booking`, after the existing source_channel/meeting_type defaults
(`~:790-795`), centralize conference resolution:

```
conf = parsed.get('conference_source')
if conf in (None, 'other') and parsed.get('conference_name_raw'):
    resolved = resolve_or_create_conference(parsed['conference_name_raw'])
    if resolved:
        conf = resolved
parsed['conference_source'] = conf
```

Only overrides `other` when a concrete bucket results — a genuinely unnamed
"other" stays `other`. Both downstream branches (existing-meeting update and
new-meeting create) already read `parsed['conference_source']` / `conf`, so no
other change needed there. Existing title-regex and date-window fallbacks stay.

### 5. Slack notice

When §3 creates a new option, post in the booking's thread:

> 🆕 New event source *BTC* created in HubSpot — reply to rename or merge.

Threaded (not channel-level) to stay low-noise. Purely informational; the
booking already succeeded.

### 6. One-off: fix Dani's existing meeting

After ship, meeting `390614433491` still reads `other`. Directly patch it once:
ensure the `btc` bucket exists, set `conference_source='btc'`, fix the title
tag `[other]` -> `[btc]`. Small script or manual PATCH — not part of the
runtime path.

## Out of scope

- The latent enum gap where `tmpcc`/`iiusa` exist in HubSpot + regex but are
  missing from the `:176` enum. One-line fix, noted but separate.
- Merging/renaming buckets from Slack replies (the notice says "reply to
  rename/merge" as a human cue; automating the merge is a later project).

## Testing

One `test_*.py`, pure-function only (no HubSpot calls):
- `slugify_conference`: "BTC 2026" -> `('btc','BTC')`; strips year; junk/empty
  -> None; punctuation collapses.
- dedup match: candidate whose normalized label equals an existing option's
  normalized label returns the existing value (assert no-create path).

## Files touched

- `meeting_bot.py` — schema field, prompt line, `slugify_conference`,
  `resolve_or_create_conference`, wire into `_process_booking`, Slack notice.
- `tests/test_conference_autocreate.py` — new.
- one-off patch for meeting `390614433491`.
