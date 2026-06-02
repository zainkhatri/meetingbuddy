#!/usr/bin/env python3
"""Repair contact `sdr_owner` to mirror `sdr` (Decision A: sdr is canonical).

`sdr` (owner-id) is the accurate "who sourced" field; `sdr_owner` (BDR-only
enum) is the dual-ownership mirror but was partly auto-set from AE territory and
is ~16% wrong. This sets `sdr_owner` = the BDR display name implied by `sdr`,
wherever `sdr` maps to a BDR and `sdr_owner` differs (empty or wrong). Non-BDR
`sdr` values are left alone (no enum option; not a BDR sourcer). Dry-run default.

Uses the LIST endpoint (no 10k search cap) + batch update (100/call).

Usage:
  HS_API_KEY=... python3 scripts/repair_sdr_owner.py            # dry-run
  HS_API_KEY=... python3 scripts/repair_sdr_owner.py --apply
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_sync

HS = {'Authorization': f'Bearer {os.environ["HS_API_KEY"]}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def all_contacts():
    after = None
    while True:
        params = {'properties': 'sdr,sdr_owner', 'limit': 100}
        if after:
            params['after'] = after
        r = requests.get('https://api.hubapi.com/crm/v3/objects/contacts',
                         headers=HS, params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        for c in d.get('results', []):
            yield c
        after = (d.get('paging') or {}).get('next', {}).get('after')
        if not after:
            return


def main():
    counts = {'seen': 0, 'fill_empty': 0, 'overwrite': 0, 'agree_skip': 0,
              'non_bdr_sdr': 0, 'no_sdr': 0}
    examples = []
    batch = []

    def flush():
        if batch and APPLY:
            requests.post('https://api.hubapi.com/crm/v3/objects/contacts/batch/update',
                          headers=HS, json={'inputs': batch}, timeout=60)
        batch.clear()

    for c in all_contacts():
        counts['seen'] += 1
        p = c['properties']
        sdr = p.get('sdr')
        cur = (p.get('sdr_owner') or '').strip()
        if not sdr:
            counts['no_sdr'] += 1
            continue
        target = sheet_sync.bdr_sdr_owner_value(sdr)
        if not target:
            counts['non_bdr_sdr'] += 1
            continue
        if cur == target:
            counts['agree_skip'] += 1
            continue
        counts['overwrite' if cur else 'fill_empty'] += 1
        if cur and len(examples) < 10:
            examples.append(f'  overwrite contact={c["id"]}: {cur!r} -> {target!r} (sdr={sdr})')
        batch.append({'id': c['id'], 'properties': {'sdr_owner': target}})
        if len(batch) >= 100:
            flush()
    flush()
    print('[repair] done', counts)
    if examples:
        print('sample overwrites:')
        print('\n'.join(examples))


if __name__ == '__main__':
    main()
