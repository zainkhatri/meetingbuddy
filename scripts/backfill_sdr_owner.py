#!/usr/bin/env python3
"""One-time backfill: set contact `sdr_owner` from the BDR who sourced their
meeting (`meeting_sourced_by`). Fill-if-empty; never overwrites. Dry-run by default.

Usage:
  HS_API_KEY=... python3 scripts/backfill_sdr_owner.py            # dry-run
  HS_API_KEY=... python3 scripts/backfill_sdr_owner.py --apply
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_sync

HS = {'Authorization': f'Bearer {os.environ["HS_API_KEY"]}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def search_sourced_meetings():
    after = None
    while True:
        body = {'filterGroups': [{'filters': [
            {'propertyName': 'meeting_sourced_by', 'operator': 'HAS_PROPERTY'}]}],
            'properties': ['meeting_sourced_by'], 'limit': 100}
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        for m in d.get('results', []):
            yield m
        after = (d.get('paging') or {}).get('next', {}).get('after')
        if not after:
            return


def contacts_for_meeting(mid):
    r = requests.get(
        f'https://api.hubapi.com/crm/v4/objects/meetings/{mid}/associations/contacts',
        headers=HS, timeout=15)
    return [str(a['toObjectId']) for a in r.json().get('results', [])] if r.status_code == 200 else []


def contact_props(cid):
    r = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                     headers=HS, params={'properties': 'sdr_owner,email'}, timeout=15)
    return (r.json().get('properties') or {}) if r.status_code == 200 else {}


def main():
    counts = {'seen_meetings': 0, 'would_set': 0, 'set': 0, 'skipped_filled': 0,
              'no_bdr': 0, 'internal': 0}
    seen_contacts = set()
    for m in search_sourced_meetings():
        counts['seen_meetings'] += 1
        owner = m['properties'].get('meeting_sourced_by')
        val = sheet_sync.bdr_sdr_owner_value(owner)
        if not val:
            counts['no_bdr'] += 1
            continue
        for cid in contacts_for_meeting(m['id']):
            if cid in seen_contacts:
                continue
            seen_contacts.add(cid)
            props = contact_props(cid)
            if (props.get('email') or '').lower().endswith(('@furtherai.com', '@further.ai')):
                counts['internal'] += 1
                continue
            if props.get('sdr_owner'):
                counts['skipped_filled'] += 1
                continue
            if APPLY:
                requests.patch(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                               headers=HS, json={'properties': {'sdr_owner': val}}, timeout=15)
                counts['set'] += 1
            else:
                counts['would_set'] += 1
                print(f'[dry] contact={cid} <- sdr_owner={val}')
    print('[backfill] done', counts)


if __name__ == '__main__':
    main()
