#!/usr/bin/env python3
"""Pure claim logic for the weekly self-serve claim button. No slack/network/env at import."""
from datetime import datetime, timezone

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
