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

### 2. Canonicalize via web lookup (best-effort)

```
canonicalize_conference(raw, meeting_date) -> {name, year} | None
```

The raw name is often a terse acronym ("BTC 2026"). Looking it up ties every
spelling/alias of the same event to one canonical name, and gives marketing a
readable label. Uses Claude's web-search server tool (Anthropic API — the
bot already holds an Anthropic client):

- Prompt: "What is the official full name of the insurance/insurtech
  conference referred to as `<raw>`? Return the canonical name and the year."
  Forced through a small tool schema so it returns `{name, year, confident}`.
- `year`: from `raw` if it carries one, else the year of `meeting_date`.
- **Best-effort, confident-only:** use the looked-up name ONLY when the tool
  returns `confident: true`. On any error, low confidence, or ambiguous result,
  fall back to the BDR's raw text. Obscure industry acronyms (e.g. BTC =
  *Broker Tech Conference*, not Bitcoin) are exactly where a generic search
  mis-guesses — so the Slack notice (§6) is ALWAYS posted on create as a
  human confirm/rename prompt, whether the label came from the lookup or the
  raw fallback.

### 3. Normalize (pure function)

```
slugify_conference(canonical_name, year) -> (value, label)
```

- `label`: `"<Canonical Name> <year>"` — **year kept**
  (e.g. "Bermuda Captive Conference 2026"). Falls back to the raw name + year
  when the lookup returned nothing ("BTC 2026").
- `value`: `<name-slug>_<year>` — name lowercased, non-alphanumeric -> `_`,
  collapse repeats, strip edge `_`, then `_<year>`
  (e.g. `bermuda_captive_conference_2026`, fallback `btc_2026`). Cap 50 chars.
- Per-year buckets: BTC 2026 and BTC 2027 are distinct options, so marketing
  attributes spend per event instance. All *aliases of the same year* collapse
  to one value via the canonicalized name.
- Returns `None` if the name part is empty or all-digits (guards junk).

> Note: legacy buckets (`tmpaa`, `wsia_uw_summit`) are year-less; auto-created
> ones are per-year. Deliberate — the year was explicitly requested for new
> events. Not retrofitting the legacy set.

### 4. Resolve-or-create (the core)

```
resolve_or_create_conference(raw, meeting_date) -> value | None
```

1. `canonicalize_conference(raw, meeting_date)` -> `{name, year}` (or fall back
   to raw + date-year). `slugify_conference(name, year)` -> candidate
   `(value, label)`. None -> return None.
2. Fetch the dropdown options (in-process cache; refresh after any create).
3. **Dedup:** normalize every existing option's `value` AND `label` the same
   way. If candidate value == an existing value, or candidate normalized-label
   == an existing normalized-label -> return the **existing** value. No create.
   (This collapses "WSIA" spelled a new way back onto `wsia_uw_summit`.)
4. Otherwise **create**: under a lock, re-fetch options (race guard), append
   `{label, value, hidden: false, displayOrder: -1}`, `PATCH` the full options
   array back to `/crm/v3/properties/meetings/conference_source` (HubSpot
   replaces the whole list — existing options, incl. hidden ones, preserved).
5. Post a Slack notice (see §6). Return the new value.

Concurrency: the live handler and the `live_sweep` thread can process bookings
simultaneously. Create runs under a dedicated lock, and re-fetches options
inside the lock, so two new-conference bookings can't double-create or clobber
each other's options.

### 5. Wire into the flow

In `_process_booking`, after the existing source_channel/meeting_type defaults
(`~:790-795`), centralize conference resolution:

```
conf = parsed.get('conference_source')
if conf in (None, 'other') and parsed.get('conference_name_raw'):
    resolved = resolve_or_create_conference(
        parsed['conference_name_raw'], parsed.get('meeting_date'))
    if resolved:
        conf = resolved
parsed['conference_source'] = conf
```

Only overrides `other` when a concrete bucket results — a genuinely unnamed
"other" stays `other`. Both downstream branches (existing-meeting update and
new-meeting create) already read `parsed['conference_source']` / `conf`, so no
other change needed there. Existing title-regex and date-window fallbacks stay.

### 6. Slack notice

**Always** posted when §4 creates a new option (not just when the lookup is
unsure) — it is the confirm/rename backstop for unreliable acronym lookups.
Post in the booking's thread:

> 🆕 New event source *Broker Tech Conference 2026* created in HubSpot —
> reply to rename or merge if that's wrong.

Threaded (not channel-level) to stay low-noise. The booking already
succeeded; this is the human confirmation step.

### 7. One-off: fix Dani's existing meeting

After ship, meeting `390614433491` still reads `other`. Directly patch it once:
ensure the `broker_tech_conference_2026` bucket exists (label "Broker Tech
Conference 2026"), set `conference_source='broker_tech_conference_2026'`, fix
the title tag `[other]` -> `[broker_tech_conference_2026]`. Small script or
manual PATCH — not part of the runtime path.

### 8. Close the tmpcc/iiusa enum gap

`tmpcc` and `iiusa` exist in HubSpot's dropdown and in `_CONF_RULES`, but are
missing from the closed `conference_source` enum at `:176`, so Claude can't
return them directly (only the regex fallback catches them). Add both to the
enum. One line.

## Out of scope

- Merging/renaming buckets from Slack replies (the notice says "reply to
  rename/merge" as a human cue; automating the merge is a later project).

## Testing

One `test_*.py`, pure-function only (no HubSpot/web calls):
- `slugify_conference`: `("BTC", 2026)` -> `('btc_2026', 'BTC 2026')`;
  punctuation collapses; empty/all-digit name -> None.
- dedup match: candidate whose normalized value/label equals an existing
  option's returns the existing value (assert no-create path).
- `canonicalize_conference`: web call is mocked/skipped — assert the raw+year
  fallback path produces a sane `{name, year}` when lookup returns nothing.

## Files touched

- `meeting_bot.py` — schema field, prompt line, `canonicalize_conference`
  (web lookup), `slugify_conference`, `resolve_or_create_conference`, wire into
  `_process_booking`, Slack notice.
- `tests/test_conference_autocreate.py` — new.
- one-off patch for meeting `390614433491`.
