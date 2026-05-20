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


def get_meeting_associations(meeting_id):
    """Return (contact, company) tuples — first associated contact + company, with properties."""
    contact = None
    company = None
    rc = requests.get(f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/contacts',
                      headers=HS, timeout=15)
    if rc.status_code == 200:
        for a in rc.json().get('results', []):
            cid = str(a['toObjectId'])
            rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                              headers=HS, params={'properties': 'firstname,lastname,jobtitle,email'}, timeout=15)
            if rg.status_code == 200:
                contact = rg.json().get('properties') or {}
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
        contact, company = get_meeting_associations(mid)
        if not company or not company.get('name'):
            continue
        payload = sheet_sync.build_payload(
            conference_slug=p.get('conference_source'),
            sourced_by_owner_id=p.get('meeting_sourced_by') or p.get('hubspot_owner_id'),
            meeting_start_ms=p.get('hs_meeting_start_time'),
            company=company.get('name'),
            contact_first=(contact or {}).get('firstname'),
            contact_last=(contact or {}).get('lastname'),
            contact_title=(contact or {}).get('jobtitle'),
            contact_email=(contact or {}).get('email'),
            hs_meeting_outcome=p.get('hs_meeting_outcome') or 'SCHEDULED',
        )
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
