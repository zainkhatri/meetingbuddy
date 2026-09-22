#!/usr/bin/env python3
"""Pure logic for BDR account recycling: cold detection + the warn→pool state
machine + digest-block building. No slack/network/env at import — every function
takes already-fetched HubSpot props and an explicit `now`, so the whole clock is
unit-testable and deterministic.

Lifecycle (per BDR-owned company; state lives in the `recycle_state` property):

    warm ──(≥WARN_DAYS cold, Thu)──▶ warned ──(≥RELEASE_DAYS, Mon)──▶ pool ──▶ (claimed)
      ▲                                 │                              │
      └──── fresh activity clears it ───┴──────────────────────────────┘

The bot layer handles the network (HubSpot search/patch, Slack DM/post) and the
deal-exclusion lookup; this module only decides what should happen.
"""
from datetime import datetime, timezone

WARN_DAYS = 27          # DM the owner at this age (3 days before release)
RELEASE_DAYS = 30       # eligible for the pool at this age
RECYCLE_CHANNEL = 'C096CHCQWJ0'   # all 5 BDRs are members
CLAIM_ACTION = 'claim_account'    # matches claim_logic's row shape

# recycle_state values
WARM = ''
WARNED = 'warned'
POOL = 'pool'


def days_since_activity(props, now):
    """Age in whole days of HubSpot 'Last Activity Date'. Accepts an epoch-millis
    string (what the search API returns) or an ISO-8601 string. Returns None when
    the field is missing/blank/unparseable — the caller treats None as 'cannot
    confirm cold' and never warns or releases on it (fail-safe)."""
    raw = (props.get('hs_last_activity_date') or '').strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            when = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        else:
            when = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return (now - when).days


def is_cold(props, now, days):
    """True only when we can confirm the account is at least `days` stale.
    Unknown activity date (None) is NOT cold — we never act on missing data."""
    d = days_since_activity(props, now)
    return d is not None and d >= days


def decide(props, now, phase, has_covered_deal=False,
           warn_days=WARN_DAYS, release_days=RELEASE_DAYS):
    """Core state machine. Pure over already-fetched props.

    phase: 'warn' (Thursday scan) or 'release' (Monday scan).
    has_covered_deal: True if the company has an open/closed-won deal (looked up
      by the bot) — such accounts are never warned/released and are pulled back
      out of any warned/pool state.

    Returns a dict: {'action', 'new_state', 'reason'} where action is one of
      'warn'   — DM the owner, set state=warned
      'release'— add to the pool digest, set state=pool
      'reset'  — clear state back to warm (owner re-engaged, or deal now covers it)
      'noop'   — do nothing
    'new_state' is None for 'noop'."""
    assert phase in ('warn', 'release'), f'bad phase: {phase!r}'
    state = (props.get('recycle_state') or WARM).strip()

    # A covered deal (open/closed-won) overrides everything: never in the program.
    if has_covered_deal:
        if state in (WARNED, POOL):
            return {'action': 'reset', 'new_state': WARM,
                    'reason': 'account now has an open/closed-won deal'}
        return {'action': 'noop', 'new_state': None, 'reason': 'covered by a deal'}

    # Owner re-engaged: any fresh activity below the warn threshold clears state.
    d = days_since_activity(props, now)
    if state in (WARNED, POOL) and d is not None and d < warn_days:
        return {'action': 'reset', 'new_state': WARM,
                'reason': f'fresh activity ({d}d ago) — owner re-engaged'}

    if phase == 'warn':
        if state == WARM and is_cold(props, now, warn_days):
            return {'action': 'warn', 'new_state': WARNED,
                    'reason': f'cold {days_since_activity(props, now)}d — warning owner'}
        return {'action': 'noop', 'new_state': None, 'reason': f'no warn (state={state or "warm"})'}

    # phase == 'release'
    if state == WARNED and is_cold(props, now, release_days):
        return {'action': 'release', 'new_state': POOL,
                'reason': f'still cold {days_since_activity(props, now)}d after warning'}
    return {'action': 'noop', 'new_state': None, 'reason': f'no release (state={state or "warm"})'}


def warn_dm_text(account_name, deadline_str, days_left):
    """The Slack DM sent to the owning BDR. Names the account, the exact deadline,
    and the one action that saves it — per the council: instructions, not anxiety."""
    account_name = (account_name or 'one of your accounts').strip()
    when = f'in {days_left} days ({deadline_str})' if days_left else f'on {deadline_str}'
    return (f"♻️ Heads up — *{account_name}* has had no activity for {WARN_DAYS}+ days, "
            f"so it goes up for grabs {when}.\n"
            f"Log any touch (call, email, meeting, or note) before then to keep it.")


def _pool_row(account):
    """One digest row: a section with a Claim button, matching the shape
    claim_logic.mark_claimed / count_company_rows expect (accessory action_id =
    claim_account, value = company id)."""
    cid = str(account['id'])
    name = account.get('name') or f'Company {cid}'
    extra = account.get('note')                       # e.g. 'cold 34d · was Ben's'
    text = f'*{name}*' + (f' — {extra}' if extra else '')
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text},
            'accessory': {'type': 'button', 'action_id': CLAIM_ACTION, 'value': cid,
                          'text': {'type': 'plain_text', 'text': 'Claim'}}}


def pool_digest_blocks(accounts, header='♻️ Up for grabs this week'):
    """Build the Monday digest blocks: a header plus one claimable row per account.
    Returns [] for an empty pool so the caller can skip posting entirely."""
    if not accounts:
        return []
    blocks = [{'type': 'header', 'text': {'type': 'plain_text', 'text': header}}]
    blocks += [_pool_row(a) for a in accounts]
    return blocks
