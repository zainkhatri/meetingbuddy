# HubSpot account history in the booking comment

**Date:** 2026-09-02
**Status:** Approved design

## Problem

When a BDR books a meeting, the bot's in-thread comment shares Claude-researched
`Segment` and `Size` (`_log_comment`). Aman asked that it also surface prior
**HubSpot history** — "if we spoke to them in the past" — so the AE walks into
the call knowing the account's context instead of treating every booking as
net-new.

## Goal

For a booked company that **already exists in HubSpot**, append an account-brief
block to the booking comment covering:

1. Prior meetings — count + most-recent date.
2. Deals — open deal (stage + amount) if any, else most recent.
3. Known contacts — how many contacts are on file for the company.
4. Owner + last activity — who owns the relationship and when it was last touched.

For a **net-new** company (the common case — the bot is often creating the
company for the first time), the comment stays byte-for-byte as it is today. No
extra API calls, no history block.

## Key correctness insight

`_log_comment` is called (lines 1197, 1304) *after* the meeting/company/deal are
created or tagged, so a naive "count meetings on this company" would include the
booking just made. The fix comes for free from ordering: `_process_booking`
already runs `co = hs_find_company(company_name)` at line 1103 — before any
writes. We snapshot history at that point, so counts reflect state *before* this
booking and no self-exclusion logic is needed. `co is None` also *is* the
net-new signal that gates the whole feature.

## Approach (A — snapshot-before-write, one helper)

Chosen over read-back-and-exclude (fiddly, more bug surface on sweep/replay
paths) and batch/GraphQL associations (new API surface, premature optimization
for a low-frequency path).

### New helper: `hs_company_history(company_id, contact_id)`

Returns a small dict, or `None` when nothing is available. Follows the existing
`hs_find_*` pattern (`requests` + the shared `HS` header dict). Four reads, all
keyed on `company_id`:

- **Prior meetings** — `POST /crm/v3/objects/meetings/search`, filter
  `associations.company EQ company_id`, property `hs_meeting_start_time`, sorted
  descending. Use `total` for the count and the first result's start time for
  "last."
- **Deals** — `POST /crm/v3/objects/deals/search`, filter
  `associations.company EQ company_id`, properties `dealname`, `dealstage`,
  `amount`. Prefer the first open deal (reuse `DEAL_OPEN_STAGES`); otherwise the
  most recent. Render name + stage + amount.
- **Known contacts** — `POST /crm/v3/objects/contacts/search`, filter
  `associations.company EQ company_id`, `limit` small. Use `total`.
- **Owner + last activity** — one `GET /crm/v3/objects/companies/{id}` with
  properties `hubspot_owner_id`, `hs_last_activity_date`. Resolve owner id →
  name via an inverted `NAME_TO_OWNER` map, falling back to a module-cached
  `GET /crm/v3/owners/{id}` for ids not in the roster.

Each of the four reads is wrapped so that a single failing call degrades to
omitting only that line — a failure never breaks the comment (bot resilience is
a first-class concern here). Return `None` if every field is empty/failed.

Return shape (all keys optional):

```python
{
    'meetings_count': int,          # > 0
    'last_meeting_date': 'YYYY-MM-DD',
    'deal': {'name': str, 'stage': str, 'amount': str|None, 'open': bool},
    'contacts_count': int,
    'owner_name': str,
    'last_activity_date': 'YYYY-MM-DD',
}
```

### Rendering in `_log_comment`

New signature: `_log_comment(parsed, is_conference, poster=None, history=None)`.
When `history` is truthy, append a block after the research lines. Only lines
with data render:

```
📋 We've spoken to *Acme* before:
• 3 prior meetings (last: 2026-06-14)
• Open deal: Acme - Intro Calls (Scheduled, $40k)
• 5 contacts on file
• Owner: Jacob · last activity 2026-08-20
```

When `history` is `None`, the returned comment is identical to today's output.

### Call-site wiring

Capture the snapshot once in `_process_booking`, guarded on `co`, near line
1104:

```python
history = hs_company_history(company_id, contact_id) if co else None
```

Pass `history` into both `_log_comment` calls (lines 1197, 1304). Sweep and
replay reuse `_process_booking`, so they inherit the behavior with no extra
wiring.

## Testing

`tests/test_company_history.py`, mocking `requests` the way the existing suite
does:

1. **Net-new** — `co is None` → `history=None` → `_log_comment` output byte-for-
   byte unchanged from today.
2. **Existing company** — mocked association responses → the block renders; the
   meeting count reflects the pre-write snapshot (excludes the just-booked
   meeting).
3. **Degradation** — one sub-read returns non-200 → that line is omitted, the
   rest of the comment still renders.

## Explicitly out of scope (YAGNI)

- Batch / GraphQL associations API.
- Any caching beyond the owner-id→name dict.
- A config toggle — the feature is always-on but self-gating (net-new pays
  nothing). Add a toggle only if the extra GETs on returning accounts become a
  latency problem.
