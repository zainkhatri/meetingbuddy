#!/usr/bin/env python3
"""One-time backfill: set deal `sdr_owner` from its associated contact's `sdr`
(Decision A: sdr is the accurate owner-id sourcing field; the contact's own
sdr_owner is territory-contaminated, so we derive from sdr instead). The owner-id
is mapped to the BDR display value the deal `sdr_owner` enum expects. Fill-if-empty;
never overwrites. Dry-run by default. Run AFTER repair_sdr_owner.py.

Complements the go-forward mechanism (a HubSpot workflow on deal-create).

Usage:
  HS_API_KEY=... python3 scripts/backfill_deal_sdr_owner.py            # dry-run
  HS_API_KEY=... python3 scripts/backfill_deal_sdr_owner.py --apply
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_sync

HS = {'Authorization': f'Bearer {os.environ["HS_API_KEY"]}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def all_deals():
    after = None
    while True:
        body = {'properties': ['sdr_owner', 'dealname'], 'limit': 100}
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/deals/search',
                          headers=HS, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        for x in d.get('results', []):
            yield x
        after = (d.get('paging') or {}).get('next', {}).get('after')
        if not after:
            return


def contacts_for_deal(did):
    r = requests.get(f'https://api.hubapi.com/crm/v4/objects/deals/{did}/associations/contacts',
                     headers=HS, timeout=15)
    return [str(a['toObjectId']) for a in r.json().get('results', [])] if r.status_code == 200 else []


def contact_sdr(cid):
    """Return the BDR display value implied by the contact's `sdr` (owner-id)."""
    r = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                     headers=HS, params={'properties': 'sdr'}, timeout=15)
    raw = (r.json().get('properties') or {}).get('sdr') if r.status_code == 200 else None
    return sheet_sync.bdr_sdr_owner_value(raw)


def main():
    counts = {'seen': 0, 'would_set': 0, 'set': 0, 'skipped_filled': 0, 'no_contact_sdr': 0}
    for d in all_deals():
        counts['seen'] += 1
        if (d['properties'].get('sdr_owner') or '').strip():
            counts['skipped_filled'] += 1
            continue
        val = None
        for cid in contacts_for_deal(d['id']):
            v = contact_sdr(cid)
            if v:
                val = v
                break
        if not val:
            counts['no_contact_sdr'] += 1
            continue
        if APPLY:
            requests.patch(f'https://api.hubapi.com/crm/v3/objects/deals/{d["id"]}',
                           headers=HS, json={'properties': {'sdr_owner': val}}, timeout=15)
            counts['set'] += 1
        else:
            counts['would_set'] += 1
            print(f'[dry] deal={d["id"]} ({d["properties"].get("dealname")!r}) <- sdr_owner={val}')
    print('[deal-backfill] done', counts)


if __name__ == '__main__':
    main()
