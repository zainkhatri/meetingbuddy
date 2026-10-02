"""Invite sweep: keep re-reading the calendar invite for open demo deals that are
still on Unassigned Territory (or a BDR) and hand each one to the AE on the invite.

The AE the BDR put on the demo owns the deal, not the account owner (Zain,
2026-10-02). The booking-path credit only looks once, right after booking, and its
retry queue is in memory, so it is lost on every 30-min restart; AEs added to the
invite later were never picked up. This sweep is the durable path.

Per deal, meetings are read in start order and the first one with any AE decides:
exactly one AE -> assign; more than one -> leave it (ambiguous). Per meeting, AEs come
from HubSpot attendee owners, then the Google invite read as its organizer, then
(when still none) the booking BDR's calendar searched at the meeting time by prospect
email, then by company name (bot-made meetings often carry neither link nor contact).
"""
from typing import Callable

import calendar_credit as cc

MAX_DEALS = 300
MAX_MEETINGS = 10
MAX_PROSPECTS = 3
# Words too generic to identify a company in a calendar title on their own.
_GENERIC = frozenset({'insurance', 'group', 'mutual', 'general', 'agency', 'services',
                      'company', 'partners', 'holdings', 'specialty', 'underwriting',
                      'great', 'american', 'national', 'united', 'first', 'programs'})


def parse_owner_ids(raw):
    """HubSpot multi-owner string '1;2' -> ['1', '2']. Pure."""
    assert raw is None or isinstance(raw, str), 'owner ids must be str|None'
    return [p.strip() for p in (raw or '').split(';') if p.strip()]


def name_terms(dealname):
    """Calendar match terms from a deal name: the full company name, plus its first
    distinctive word (>=4 letters, not generic) so 'Burns and Wilcox' matches
    'Burns & Wilcox / FurtherAI'. Pure."""
    assert dealname is None or isinstance(dealname, str), 'dealname must be str|None'
    base = (dealname or '').split(' - ')[0].strip().lower().rstrip('.')
    if not base:
        return []
    terms = [base]
    for w in base.split()[:6]:
        if len(w) >= 4 and w.isalpha() and w not in _GENERIC and w != 'the':
            if w != base:
                terms.append(w)
            break
    return terms


def aes_for_meeting(m, *, ae_email_map, owner_email_fn, resolve_fn, decode_fn):
    """AE owner ids on one meeting (set, possibly empty). Never raises on a miss."""
    assert isinstance(m, dict), 'meeting must be a dict'
    assert callable(resolve_fn) and callable(decode_fn), 'resolve_fn/decode_fn required'
    aes = {o for o in (m.get('attendee_owner_ids') or []) if o in cc.AE_IDS}
    url, start = m.get('external_url'), m.get('start_iso')
    _, organizer = decode_fn(url or '')
    if organizer and organizer.lower().endswith('@furtherai.com'):
        found = resolve_fn(booker_email=organizer.lower(), prospect_email=None,
                           external_url=url, start_iso=start, ae_email_map=ae_email_map)
        aes |= found or set()
    booker = owner_email_fn(m.get('booker_owner_id')) if m.get('booker_owner_id') else None
    if aes or not booker or not start:
        return aes
    terms = (m.get('prospect_emails') or [])[:MAX_PROSPECTS] + (m.get('name_terms') or [])[:2]
    for pe in terms:
        found = resolve_fn(booker_email=booker, prospect_email=pe, external_url=None,
                           start_iso=start, ae_email_map=ae_email_map)
        if found:
            return aes | found
    return aes


def decide(ae_sets):
    """('assign', ae) | ('skip', 'no_ae'|'multi_ae'). Earliest meeting with an AE wins."""
    assert isinstance(ae_sets, list), 'ae_sets must be a list'
    for aes in ae_sets[:MAX_MEETINGS]:
        if len(aes) == 1:
            return ('assign', next(iter(aes)))
        if len(aes) > 1:
            return ('skip', 'multi_ae')
    return ('skip', 'no_ae')


def _sweep_deal(deal, *, fetch_meetings, aes_fn, read_owner, assign_fn, log_fn,
                assign_enabled):
    res = {'deal_id': deal['id'], 'name': deal.get('name'), 'action': 'skip',
           'reason': None, 'assigned_to': None, 'wrote': False}
    meetings = (fetch_meetings(deal) or [])[:MAX_MEETINGS]
    action, payload = decide([aes_fn(m) for m in meetings])
    if action == 'skip':
        res['reason'] = payload
        return res
    res['action'], res['assigned_to'] = 'assign', payload
    cur = read_owner(deal) or ''
    # Same guard as the booking path: only Unassigned/ownerless or BDR-owned deals.
    can_write = cur in ('', cc.UNASSIGNED) or cur in cc.BDR_IDS
    if not can_write:
        res['reason'] = 'claimed'
        return res
    if assign_enabled:
        assign_fn(deal_id=deal['id'], owner_id=payload)
        log_fn(deal['id'], cur, payload, 'assign', 'sweep')
        res['wrote'] = True
    return res


def sweep_once(*, fetch_deals: Callable, fetch_meetings: Callable, aes_fn: Callable,
               read_owner: Callable, assign_fn: Callable, log_fn: Callable,
               assign_enabled: bool) -> list:
    """One pass over the candidate deals. Returns one result dict per deal."""
    assert callable(fetch_deals) and callable(fetch_meetings), 'fetchers required'
    assert callable(assign_fn) and callable(log_fn), 'assign_fn/log_fn required'
    out = []
    for deal in (fetch_deals() or [])[:MAX_DEALS]:
        try:
            out.append(_sweep_deal(deal, fetch_meetings=fetch_meetings, aes_fn=aes_fn,
                                   read_owner=read_owner, assign_fn=assign_fn,
                                   log_fn=log_fn, assign_enabled=assign_enabled))
        except Exception as e:                      # one bad deal never stops the pass
            print(f'[invite-sweep] deal {deal.get("id")}: {e}', flush=True)
            out.append({'deal_id': deal.get('id'), 'name': deal.get('name'),
                        'action': 'error', 'reason': str(e)[:200],
                        'assigned_to': None, 'wrote': False})
    return out
