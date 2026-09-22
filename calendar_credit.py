"""Calendar-based AE crediting for BDR-booked demos.

At booking time, credit the demo's deal to the AE actually on the Google Calendar
invite, and nudge the BDR in Slack on ambiguity/conflict. Gated behind
CREDIT_BY_CALENDAR (default off). See docs/superpowers/specs/2026-09-18-...md.

House style mirrors vp_escalation.py: validated params, >=2 assertions/fn,
bounded loops, idempotent writes.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

# AE owner ids. Duplicated from crm/hubspot/live/route_meeting_deals.py (that cron
# lives in a different repo and cannot import this module). Keep the two in sync.
AE_IDS = frozenset({'163071452', '96605305', '162894707', '84250910', '165453251',
                    '166089614', '165453250', '654909503', '164601691'})

# BDR owner ids (Matt, Dani, Jacob, Zain, Ben) — also mirrored from that cron. A
# demo deal may be auto-credited only when it is Unassigned/ownerless OR still owned
# by the booking BDR; a deal claimed by ANY other human (a non-AE manager/ops/founder)
# is never overwritten — only its AE, if any, is trusted. Keep in sync.
BDR_IDS = frozenset({'92184259', '82377567', '162210484', '88760040', '164943105'})


def decide_action(incumbent_ae: Optional[str], ae_on_invite):
    """Pure decision. Returns ('assign', ae_id) | ('nudge', reason) | ('noop', None).

    reason in {'no_ae','multi_ae','incumbent_conflict'}. Takes an already-resolved
    set (never None — the invite-lag/None case is handled by the caller before this)."""
    assert incumbent_ae is None or isinstance(incumbent_ae, str), 'incumbent_ae must be str|None'
    assert isinstance(ae_on_invite, (set, frozenset)), 'ae_on_invite must be a set'
    if incumbent_ae:
        if ae_on_invite == {incumbent_ae}:
            return ('noop', None)
        return ('nudge', 'incumbent_conflict')
    if len(ae_on_invite) == 1:
        return ('assign', next(iter(ae_on_invite)))
    if not ae_on_invite:
        return ('nudge', 'no_ae')
    return ('nudge', 'multi_ae')


def nudge_text(reason: str, incumbent_name: Optional[str] = None) -> str:
    """Slack message for a nudge decision. Pure."""
    assert reason in ('no_ae', 'multi_ae', 'incumbent_conflict'), f'bad reason: {reason}'
    assert incumbent_name is None or isinstance(incumbent_name, str), 'name must be str|None'
    if reason == 'no_ae':
        return ("⚠️ No AE is on this demo's calendar invite — add the AE who should "
                "own it so the deal gets credited (it's Unassigned for now).")
    if reason == 'multi_ae':
        return ("⚠️ Multiple AEs are on this demo's invite — I can't tell who owns it, "
                "so I left the deal Unassigned. Leave only the owning AE on the invite.")
    who = incumbent_name or 'Another AE'
    return (f"ℹ️ *{who}* already owns an open deal for this company — best to put "
            f"{who} on the call rather than credit someone else.")


# Railway volume is mounted at /data; override for tests/local via CREDIT_AUDIT_PATH.
_AUDIT_PATH = os.environ.get('CREDIT_AUDIT_PATH', '/data/credit_audit.jsonl')


def log_owner_change(deal_id, prior_owner, new_owner, reason, source,
                     *, path=None, ts_fn=None) -> dict:
    """Append one ownership-change record (JSONL). Returns the record.
    Never raises on write failure — logs and continues."""
    assert deal_id, 'deal_id required'
    assert source in ('booking', 'cron'), 'source must be booking|cron'
    ts_fn = ts_fn or time.time
    path = path or _AUDIT_PATH
    rec = {'ts': round(ts_fn(), 3), 'deal_id': str(deal_id),
           'prior_owner': prior_owner or '', 'new_owner': new_owner or '',
           'reason': reason, 'source': source}
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')
    except Exception as e:                              # never break the booking flow
        print(f'[credit-audit] write failed: {e}', flush=True)
    return rec


_OWNERS_URL = 'https://api.hubapi.com/crm/v3/owners'


def build_ae_email_map(http_get: Optional[Callable] = None,
                       api_key: Optional[str] = None) -> dict:
    """Lowercased AE email -> owner id, restricted to AE_IDS. Paginated, bounded."""
    if http_get is None:
        import requests
        http_get = requests.get
    api_key = api_key or os.environ.get('HS_API_KEY')
    assert api_key, 'HS_API_KEY required'
    assert callable(http_get), 'http_get must be callable'
    hdr = {'Authorization': f'Bearer {api_key}'}
    out, after = {}, None
    for _ in range(20):                              # <=2000 owners, hard cap
        params = {'limit': 100}
        if after:
            params['after'] = after
        r = http_get(_OWNERS_URL, headers=hdr, params=params, timeout=30)
        if not (r is not None and getattr(r, 'ok', False)):
            break
        j = r.json()
        for o in j.get('results', []):
            oid = str(o.get('id') or '')
            em = (o.get('email') or '').strip().lower()
            if oid in AE_IDS and em:
                out[em] = oid
        after = j.get('paging', {}).get('next', {}).get('after')
        if not after:
            break
    return out


def resolve_ae_from_calendar(booker_email, prospect_email, external_url, start_iso,
                             ae_email_map, *, token_fn=None, http_get=None,
                             decode_fn=None, search_fn=None):
    """AE owner ids on the demo's invite, or None when unknown.

    None means: calendar unconfigured/impersonation denied, OR the event can't be
    found yet (invite-lag) -> caller should retry, not nudge. An empty set means the
    event was found but no AE is on it -> a real 'no_ae' signal."""
    assert isinstance(ae_email_map, dict), 'ae_email_map must be a dict'
    assert booker_email and '@' in booker_email, 'valid booker_email required'
    import vp_escalation as vp
    if token_fn is None:
        token_fn = vp._gcal_token
    if http_get is None:
        import requests
        http_get = requests.get
    if decode_fn is None:
        decode_fn = vp.decode_event_url
    if search_fn is None:
        search_fn = vp.find_calendar_event

    token = token_fn(subject=booker_email)
    if not token:
        return None                                  # unconfigured / denied

    eid, organizer = decode_fn(external_url or '')
    cal = organizer or booker_email
    if not eid:                                      # URL absent -> fallback search
        terms = [prospect_email] if prospect_email else []
        eid, organizer = search_fn(booker_email, start_iso, terms)
        cal = organizer or booker_email
        if not eid:
            return None                              # invite not synced -> retry

    r = http_get(f'{vp._CAL_API}/calendars/{cal}/events/{eid}',
                 headers={'Authorization': f'Bearer {token}'}, timeout=15)
    if not (r is not None and getattr(r, 'ok', False)):
        return None
    aes = set()
    for a in (r.json().get('attendees', []) or [])[:50]:   # bounded
        em = (a.get('email') or '').strip().lower()
        if em.endswith('@furtherai.com') and em in ae_email_map:
            aes.add(ae_email_map[em])
    return aes


# Invite-lag retry: the GCal invite usually isn't synced at booking time, so a
# first resolve returns None. Enqueue and retry until RETRY_TTL_SEC. In-memory
# (Railway disk is ephemeral); the cron in the other repo is the ultimate backstop.
RETRY_TTL_SEC = 45 * 60
_RETRY: list = []
_RETRY_LOCK = threading.Lock()


def retry_enqueue(ctx: dict, *, now_fn=None) -> None:
    """Queue a booking for a later credit retry. Deduped by meeting_id."""
    assert isinstance(ctx, dict), 'ctx must be a dict'
    assert ctx.get('meeting_id'), 'meeting_id required'
    now_fn = now_fn or time.time
    with _RETRY_LOCK:
        for e in _RETRY:                                  # bounded by booking volume
            if e['meeting_id'] == ctx['meeting_id']:
                return
        ctx.setdefault('first_seen', now_fn())
        _RETRY.append(ctx)


def retry_pending(*, now_fn=None) -> list:
    """Non-expired pending items; prune expired ones in place."""
    now_fn = now_fn or time.time
    now = now_fn()
    with _RETRY_LOCK:
        _RETRY[:] = [e for e in _RETRY if now - e['first_seen'] < RETRY_TTL_SEC]
        return list(_RETRY)


def retry_remove(meeting_id) -> None:
    assert meeting_id, 'meeting_id required'
    with _RETRY_LOCK:
        _RETRY[:] = [e for e in _RETRY if e['meeting_id'] != meeting_id]


def run_retry_once(*, credit_fn, now_fn=None) -> int:
    """Retry each pending booking once via credit_fn(ctx). Remove any that no longer
    return 'retry'. Returns the count still pending. Bounded by queue size."""
    assert callable(credit_fn), 'credit_fn must be callable'
    pending = retry_pending(now_fn=now_fn)
    for ctx in pending[:200]:                       # bounded
        try:
            out = credit_fn(ctx)
        except Exception as e:
            print(f'[credit-retry] {ctx.get("meeting_id")}: {e}', flush=True)
            continue
        if not out or out.get('action') != 'retry':
            retry_remove(ctx['meeting_id'])
    return len(retry_pending(now_fn=now_fn))


UNASSIGNED = '166833455'


def credit_after_booking(*, meeting_id, deal_id, deal_owner_id, incumbent_ae,
                         booker_email, prospect_email, external_url, start_iso,
                         ae_email_map, owner_name_fn, assign_enabled=False,
                         assign_fn=None, resolve_fn=None, now_fn=None) -> dict:
    """Resolve the invite and apply the decision. Returns an action dict; never raises.

    On None (invite-lag) -> enqueue retry, no text. On assign -> write owner only when
    assign_enabled AND the deal is Unassigned/BDR-owned (never overrides a genuine AE)."""
    assert booker_email and '@' in booker_email, 'valid booker_email required'
    assert callable(owner_name_fn), 'owner_name_fn must be callable'
    resolve_fn = resolve_fn or resolve_ae_from_calendar
    result = {'action': 'noop', 'text': None, 'assigned_to': None, 'wrote': False}
    try:
        ae_on_invite = resolve_fn(
            booker_email=booker_email, prospect_email=prospect_email,
            external_url=external_url, start_iso=start_iso, ae_email_map=ae_email_map)
    except Exception as e:
        print(f'[credit] resolve failed for mtg {meeting_id}: {e}', flush=True)
        ae_on_invite = None
    if ae_on_invite is None:
        retry_enqueue({'meeting_id': meeting_id, 'deal_id': deal_id,
                       'deal_owner_id': deal_owner_id, 'incumbent_ae': incumbent_ae,
                       'booker_email': booker_email, 'prospect_email': prospect_email,
                       'external_url': external_url, 'start_iso': start_iso}, now_fn=now_fn)
        result['action'] = 'retry'
        return result

    action, payload = decide_action(incumbent_ae, ae_on_invite)
    if action == 'noop':
        return result
    if action == 'nudge':
        name = owner_name_fn(incumbent_ae) if payload == 'incumbent_conflict' else None
        result['action'] = 'nudge'
        result['text'] = nudge_text(payload, incumbent_name=name)
        return result
    # action == 'assign'
    result['action'] = 'assign'
    result['assigned_to'] = payload
    # Only credit an Unassigned/ownerless deal or one still owned by the booking
    # BDR. NEVER overwrite a human who deliberately claimed it (a non-AE manager/
    # ops/founder is not in AE_IDS but must be left alone) — that would silently
    # move a commission-bearing deal off its rightful owner.
    can_write = (deal_owner_id in ('', UNASSIGNED)) or (deal_owner_id in BDR_IDS)
    if assign_enabled and can_write and callable(assign_fn):
        assign_fn(deal_id=deal_id, owner_id=payload)
        log_owner_change(deal_id, deal_owner_id, payload, 'assign', 'booking')
        result['wrote'] = True                      # a real owner write happened
    return result
