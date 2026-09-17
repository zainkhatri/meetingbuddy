#!/usr/bin/env python3
"""Pure claim logic for the weekly self-serve claim button. No slack/network/env at import."""
import math
from datetime import datetime, timezone

NBDRS = 5                      # even-split cap: each BDR may claim ceil(digest_size / NBDRS)
CLAIMED_MARK = '✅ Claimed by '

# Slack user id -> SDR first name (the 5 valid sdr_owner enum values). Looked up 2026-09-16.
SDR_SLACK = {
    'U0AGP9NCBA5': 'Zain',
    'U0ADR5W8Q10': 'Jacob',    # Jacob Sanders
    'U099VBSUFPD': 'Dani',     # Daniella Salgado
    'U0B5J4MDC4T': 'Ben',      # Ben Trotter
    'U0B2WK6G3R9': 'Matt',     # Matt Stapleton
}
SDR_SLACK_REV = {name: uid for uid, name in SDR_SLACK.items()}

def _claim_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def claim_decision(props, sdr):
    """Pure. props = current HubSpot company props; sdr = claiming SDR name.
    Returns (ok, patch) or (False, {'reason': ...})."""
    prev = (props.get('sdr_owner') or '').strip()
    status = (props.get('recycle_status') or '').strip()
    if prev == sdr:
        return False, {'reason': "You already own this one."}
    if status == 'active' and (props.get('last_claim_by') or '').strip():
        return False, {'reason': f"Already claimed by {props.get('last_claim_by')}."}
    return True, {'sdr_owner': sdr, 'recycle_status': 'active', 'claim_date': _claim_iso(),
                  'last_claim_by': sdr, 'claimed_from': prev}


# --- digest message helpers (even-split cap + claimed-row rewrite) ---

def _is_company_row(b):
    """A digest row: a section that still has a Claim button OR is already marked claimed."""
    if b.get('type') != 'section':
        return False
    if (b.get('accessory') or {}).get('action_id') == 'claim_account':
        return True
    return CLAIMED_MARK in (b.get('text') or {}).get('text', '')

def count_company_rows(blocks):
    return sum(1 for b in blocks if _is_company_row(b))

def count_claimed_by(blocks, sdr):
    tag = CLAIMED_MARK + sdr
    return sum(1 for b in blocks
               if b.get('type') == 'section' and tag in (b.get('text') or {}).get('text', ''))

def claim_cap(total_rows, n_bdrs=NBDRS):
    """Even split: each BDR may claim ceil(total_rows / n_bdrs) from this digest (min 1)."""
    return max(1, math.ceil(total_rows / n_bdrs)) if total_rows else 0

def cap_ok(blocks, sdr, n_bdrs=NBDRS):
    """Returns (ok, cap, used). ok is False when sdr is already at/over their even-split cap."""
    total = count_company_rows(blocks)
    cap = claim_cap(total, n_bdrs)
    used = count_claimed_by(blocks, sdr)
    return (used < cap, cap, used)

def mark_claimed(blocks, cid, sdr):
    """Return new blocks with the row whose Claim button value==cid rewritten to show it's
    claimed by sdr, and its button removed. No-op if the row/button isn't found."""
    out = []
    for b in blocks:
        acc = b.get('accessory') or {}
        if (b.get('type') == 'section' and acc.get('action_id') == 'claim_account'
                and str(acc.get('value')) == str(cid)):
            nb = {k: v for k, v in b.items() if k != 'accessory'}
            base = (b.get('text') or {}).get('text', '')
            nb['text'] = {'type': 'mrkdwn', 'text': f'{base}  ·  {CLAIMED_MARK}{sdr}'}
            out.append(nb)
        else:
            out.append(b)
    return out
