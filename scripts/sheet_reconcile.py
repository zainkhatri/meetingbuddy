#!/usr/bin/env python3
"""Daily reconciler: HubSpot meetings → Ellen's "Full Meeting Tracker" sheet.

Pulls meetings created/updated in the last N days that have a non-empty
conference_source, builds the same payload meeting_bot.py would, and upserts
each into the sheet. Safety net for the inline writes done by meeting_bot.py:
catches transient Sheets API failures and meetings created manually in HubSpot
(via GCal sync) that the bot never saw.

Usage:
  python3 scripts/sheet_reconcile.py            # last 7 days
  python3 scripts/sheet_reconcile.py --since 30 # last 30 days
  python3 scripts/sheet_reconcile.py --dry-run  # print actions, no writes
"""
import argparse
import os
import sys
import time

import requests

APOLLO_API_KEY = os.environ.get('APOLLO_API_KEY')


def apollo_enrich(first, last, company, email=None):
    """Look up contact in Apollo. Returns {'email': str|None, 'phone': str|None}.
    Returns empty dict on miss or any error (best-effort enrichment)."""
    if not APOLLO_API_KEY:
        return {}
    if not (email or (first and last and company)):
        return {}
    # reveal_phone_number requires async webhook + Apollo CRM contact; we only
    # do synchronous email enrichment here. Phone enrichment lives in
    # conference_buddy's fill_phones_sync.py for now.
    body = {'reveal_personal_emails': True}
    if email: body['email'] = email
    if first: body['first_name'] = first
    if last: body['last_name'] = last
    if company: body['organization_name'] = company
    try:
        r = requests.post('https://api.apollo.io/v1/people/match',
                          headers={'Content-Type': 'application/json', 'X-Api-Key': APOLLO_API_KEY},
                          json=body, timeout=15)
        if not r.ok:
            return {}
        person = (r.json() or {}).get('person') or {}
    except Exception:
        return {}
    out = {}
    em = person.get('email') or (person.get('contact') or {}).get('email')
    if em and '@' in em and 'email_not_unlocked' not in em.lower():
        out['email'] = em
    phones = []
    if isinstance(person.get('phone_numbers'), list):
        phones += person['phone_numbers']
    contact = person.get('contact') if isinstance(person.get('contact'), dict) else None
    if contact and isinstance(contact.get('phone_numbers'), list):
        phones += contact['phone_numbers']
    # Personal lines first. HQ = company switchboard — useless, skip entirely.
    type_rank = {'mobile': 0, 'work_direct': 1, 'work': 2, 'home': 3}
    keep = [p for p in phones if (p.get('type') or '').lower() != 'hq']
    keep.sort(key=lambda x: type_rank.get((x.get('type') or '').lower(), 9))
    seen, ordered = set(), []
    for p in keep:
        s = (p.get('sanitized_number') or p.get('raw_number') or '').strip()
        if s and s not in seen:
            seen.add(s); ordered.append(s)
    if ordered:
        out['phone'] = ordered[0]
        out['phone_type'] = (keep[0].get('type') or '').lower() if keep else ''
    return out

# Allow `import sheet_sync` regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_sync

HS_API_KEY = os.environ['HS_API_KEY']
HS = {'Authorization': f'Bearer {HS_API_KEY}', 'Content-Type': 'application/json'}

MEETING_PROPS = [
    'hs_meeting_title',
    'hs_meeting_start_time',
    'hs_meeting_outcome',
    'meeting_sourced_by',
    'conference_source',
    'hubspot_owner_id',
]


def search_meetings(since_ms):
    """Yield HubSpot meeting records with conference_source set, started since `since_ms`."""
    after = None
    while True:
        body = {
            'filterGroups': [{'filters': [
                {'propertyName': 'conference_source', 'operator': 'HAS_PROPERTY'},
                {'propertyName': 'hs_meeting_start_time', 'operator': 'GTE', 'value': str(since_ms)},
            ]}],
            'properties': MEETING_PROPS,
            'limit': 100,
        }
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        for m in data.get('results', []):
            yield m
        after = (data.get('paging') or {}).get('next', {}).get('after')
        if not after:
            return


INTERNAL_EMAIL_DOMAINS = ('@furtherai.com', '@further.ai')

# Real BDR roster — only meetings sourced by (or owned by) these get into Ellen's sheet.
# Per Zain's stance: AEs / Aman / reps may attend but don't source for the BDR dashboard.
BDR_OWNER_IDS = {'88760040', '162210484', '82377567',  # Zain, Jacob, Dani
                 '164943105', '92184259'}              # Ben Trotter, Matt Stapleton


def _is_internal(email):
    e = (email or '').lower()
    return any(d in e for d in INTERNAL_EMAIL_DOMAINS)


def get_meeting_associations(meeting_id):
    """Return (contact, company) — first NON-INTERNAL associated contact + company.
    Skips contacts with @furtherai.com / @further.ai emails (BDRs / team)."""
    contact = None
    company = None
    rc = requests.get(f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/contacts',
                      headers=HS, timeout=15)
    if rc.status_code == 200:
        for a in rc.json().get('results', []):
            cid = str(a['toObjectId'])
            rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                              headers=HS, params={'properties': 'firstname,lastname,jobtitle,email'}, timeout=15)
            if rg.status_code != 200:
                continue
            props = rg.json().get('properties') or {}
            if _is_internal(props.get('email')):
                continue  # skip internal teammates
            contact = props
            break
    rco = requests.get(f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/companies',
                       headers=HS, timeout=15)
    if rco.status_code == 200:
        for a in rco.json().get('results', []):
            coid = str(a['toObjectId'])
            rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/companies/{coid}',
                              headers=HS, params={'properties': 'name'}, timeout=15)
            if rg.status_code == 200:
                company = rg.json().get('properties') or {}
                break
    return contact, company


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', type=int, default=7, help='Days back to reconcile (default 7)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    since_ms = int((time.time() - args.since * 86400) * 1000)
    print(f'[reconcile] scanning HubSpot meetings since {args.since}d ago (ms={since_ms})')

    counts = {'inserted': 0, 'updated': 0, 'skipped': 0, 'error': 0}
    seen = 0
    for m in search_meetings(since_ms):
        seen += 1
        mid = m['id']
        p = m.get('properties') or {}
        if (p.get('hs_meeting_outcome') or '') in ('CANCELED', 'NO_SHOW'):
            continue
        # Only sync meetings owned/sourced by a real BDR
        src = str(p.get('meeting_sourced_by') or '')
        own = str(p.get('hubspot_owner_id') or '')
        if src not in BDR_OWNER_IDS and own not in BDR_OWNER_IDS:
            counts['skipped'] += 1
            continue
        try:
            contact, company = get_meeting_associations(mid)
        except Exception as e:
            print(f'[skip] meeting={mid} assoc lookup failed: {e}')
            counts['error'] += 1
            continue
        if not company or not company.get('name'):
            continue
        c = contact or {}
        first = c.get('firstname'); last = c.get('lastname')
        email = c.get('email'); title = c.get('jobtitle')
        co_name = company.get('name')
        # Apollo enrichment: fill missing email + grab phone for Notes
        apollo_phone = ''
        if not email and (first and last and co_name):
            data = apollo_enrich(first, last, co_name, None)
            if data.get('email'): email = data['email']
            if data.get('phone'): apollo_phone = data['phone']
        elif email and APOLLO_API_KEY:
            # We have email; still grab phone via Apollo match for richer rows
            data = apollo_enrich(first, last, co_name, email)
            if data.get('phone'): apollo_phone = data['phone']

        payload = sheet_sync.build_payload(
            conference_slug=p.get('conference_source'),
            sourced_by_owner_id=p.get('meeting_sourced_by') or p.get('hubspot_owner_id'),
            meeting_start_ms=p.get('hs_meeting_start_time'),
            company=co_name,
            contact_first=first,
            contact_last=last,
            contact_title=title,
            contact_email=email,
            hs_meeting_outcome=p.get('hs_meeting_outcome') or 'SCHEDULED',
        )
        if apollo_phone:
            payload['Notes'] = f'Phone: {apollo_phone}'
        if args.dry_run:
            print(f'[dry] {payload["Conference"]} | {payload["Prospect Company"]} | {payload["Meeting Date"]} | {payload["Prospect Name"]}')
            counts['skipped'] += 1
            continue
        result = sheet_sync.upsert_meeting_row(payload)
        action = result.get('action', 'error')
        counts[action] = counts.get(action, 0) + 1
        if action == 'error':
            print(f'[err] meeting={mid} {result.get("error")}')

    print(f'[reconcile] done. seen={seen} {counts}')


if __name__ == '__main__':
    main()
