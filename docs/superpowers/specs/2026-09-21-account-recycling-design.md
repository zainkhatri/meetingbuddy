# Account Recycling — cold-clock → warn → weekly pool (design)

**Date:** 2026-09-21
**Status:** Approved design, pre-implementation
**Owner:** Zain

## Problem

The BDR account-recycling program was announced ("live") but only the *claim*
half exists in code. There is no machinery to detect cold accounts, warn the
owning BDR before an account is taken, or post the weekly "up for grabs" pool.
The announcement also landed in an AE-only channel where no BDR can claim, and
the bot is not a member of the intended BDR channel.

This spec defines the missing pipeline: detect a BDR's account going cold, warn
the owner, and — if still cold — surface it in a weekly claimable pool.

## Program rules (from #ae-team recycle thread)

- An account with no contact for 30 days is eligible for recycling.
- The owner is warned ~3 days before release ("contact this in 3 days or you
  lose it").
- If the owner re-engages, the account stays theirs. Otherwise any BDR can claim
  it from the weekly pool.

## Decisions (locked)

| Question | Decision |
|---|---|
| What defines "cold"? | HubSpot **Last Activity Date** — resets on any logged email, call, meeting, or note. |
| Warn method | **Slack DM** from the bot to the owning BDR. |
| Release model | **Soft release** — post to the pool with a Claim button; owner keeps ownership until someone actually claims. |
| Cadence | **Weekly Monday batch** — one digest per week. Aligns with the existing weekly claim cap ("More open up next Monday"). |
| Scope | **BDR-owned accounts only** (`sdr_owner` ∈ the 5 BDRs), excluding any with an open/closed-won deal. Unowned accounts are ignored. |
| Target channel | **C096CHCQWJ0** (all 5 BDRs are members). |

## Lifecycle

```
warm ──(≥27d no activity, Thu scan)──▶ warned ──(≥30d, still cold Monday)──▶ pool ──(claimed)──▶ warm (new owner)
  ▲                                       │                             │
  └──────── owner logs any activity ──────┴─────────────────────────────┘   (clears recycle_state; stays theirs)
```

State lives in a new HubSpot company property **`recycle_state`** with values
`''` (warm), `warned`, `pool`.

## Components

### 1. `recycle.py` (new, pure logic — mirrors `claim_logic.py`)

No network/env at import. Unit-testable. Responsibilities:

- `is_cold(props, now, days)` — True when `hs_last_activity_date` is ≥ `days`
  old. Pure over already-fetched props.
- `next_state(props, phase, now)` — given the current `recycle_state`, activity
  recency, and the job phase (`warn` | `release`), returns the target state and
  whether to act (DM / post / reset). Encodes every transition in one place.
- `warn_dm_text(account_name, days_left)` — the DM copy.
- `pool_digest_blocks(accounts)` — build the Slack blocks for the Monday digest
  (one `section` + `claim_account` button per account), reusing the row shape
  `claim_logic`/`mark_claimed` already expect.

### 2. Wiring in `meeting_bot.py` (thin)

Two scheduled entry points, following the existing `*_sweep_loop` pattern and
guarded like the other loops:

- **`recycle_warn_scan()` — Thursday.** Search HubSpot companies:
  `sdr_owner` ∈ BDRs, `hs_last_activity_date` ≥ 27 days old, `recycle_state`
  not already `warned`/`pool`, **and** `hs_company_has_covered_deal` is False.
  For each: DM the owner (via `SDR_SLACK_REV`), set `recycle_state = warned`.
  If a previously-warned account now has fresh activity, clear `recycle_state`.
- **`recycle_release_and_post()` — Monday.** Search companies with
  `recycle_state = warned` still cold (≥30d) and no covered deal. Build one
  digest via `pool_digest_blocks`, post to `RECYCLE_CHANNEL` (C096CHCQWJ0), set
  each `recycle_state = pool`. Newly-warm accounts get `recycle_state` cleared
  and are dropped from the batch.

Scheduling: reuse the process's existing loop/watchdog approach; gate the
day-of-week/time check inside the function so a missed tick self-heals on the
next run (consistent with `periodic_restart` + replay philosophy).

### 3. Claim handler (existing, small addition)

`handle_claim_account` already reassigns `sdr_owner`, records `claimed_from`,
enforces the even-split weekly cap, and (as of 2026-09-21) rejects accounts with
an open/closed-won deal. Add one line: on a successful claim, clear
`recycle_state` so the account returns to `warm` under the new owner.

## Data flow

```
Thu  recycle_warn_scan ─▶ HubSpot search (BDR-owned, ≥27d, not warned, no deal)
                        ─▶ per account: Slack DM owner + set recycle_state=warned
Mon  recycle_release_and_post ─▶ HubSpot search (recycle_state=warned, ≥30d, no deal)
                              ─▶ pool_digest_blocks ─▶ chat_postMessage to C096CHCQWJ0
                              ─▶ set recycle_state=pool
click Claim ─▶ handle_claim_account ─▶ reassign sdr_owner, clear recycle_state
```

## Error handling

- **HubSpot search / patch failures:** log and skip that account; the weekly
  cadence retries next cycle. Never crash the loop (matches existing loops).
- **Covered-deal check returns None (API error):** treat as "cannot confirm" →
  do **not** warn or release that account this cycle (fail safe toward Gavin's
  rule). Reuses the existing `hs_company_has_covered_deal` tri-state.
- **DM failure (owner unmappable / Slack error):** log; still set `warned` so the
  account isn't stuck warm forever. (A BDR with no `SDR_SLACK` mapping is a data
  gap to fix, not a reason to skip recycling.)
- **Bot not in channel:** the Monday post fails loudly in logs. Operational
  prerequisite: invite the bot to C096CHCQWJ0 before first run.

## Testing

- `recycle.py` pure functions get a `test_recycle.py` sibling to
  `test_claim_decision.py`: cold/warm boundary, every `next_state` transition
  (warm→warned, warned→pool, warned→warm on re-engage, pool untouched),
  digest-block shape round-trips through `count_company_rows`/`mark_claimed`.
- The two `meeting_bot.py` entry points are thin orchestration over pure logic +
  HubSpot/Slack calls — covered by the pure tests plus a manual dry-run before
  enabling.

## Prerequisites & operational notes

1. Create HubSpot company property `recycle_state` (enumeration: ``, `warned`,
   `pool`).
2. Invite the meeting bot to **C096CHCQWJ0**.
3. Confirm the HubSpot recycling workflow (the "APP" post) is disabled or
   repointed so we don't double-post; this bot becomes the single source of the
   pool digest (required — Slack routes button clicks to the posting app).
4. Re-confirm Gavin's exclusion also holds in whatever selection the workflow did
   before; this design excludes covered deals at both scan and claim time.

## Tunable / deferred (YAGNI for v1)

- **Day thresholds:** 27 warn / 30 release. Warn runs Thursday for the Monday
  digest (~3 days incl. weekend). Both are constants, easy to shift.
- **Unclaimed accounts:** posted once and left in the pool until claimed or the
  owner re-engages (which clears state). Weekly re-surfacing of stale unclaimed
  accounts is deliberately out of scope for v1.
- **SMS warning:** Slack DM only. SMS would need a provider + phone numbers;
  deferred.

## Out of scope

- Editing the HubSpot workflow itself (done in HubSpot, not this repo).
- General lead distribution of unowned accounts.
- The AE-crediting work on the current branch (separate feature).
