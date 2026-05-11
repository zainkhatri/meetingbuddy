"""Backfill: walk meetings booked in the last N days, find each meeting's
associated contact, look up the contact's primary company, and add the
meeting->company association if missing.

Usage:
    railway run --service meetingbuddy python3 -u backfill_assocs.py [DAYS]
"""
import os
import sys
import datetime
import requests

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
HS = {'Authorization': f'Bearer {os.environ["HS_API_KEY"]}', 'Content-Type': 'application/json'}

now = datetime.datetime.now(datetime.timezone.utc)
start_ms = int((now - datetime.timedelta(days=DAYS)).timestamp() * 1000)


def search_meetings():
    after = None
    while True:
        body = {
            'filterGroups': [{'filters': [
                {'propertyName': 'booked_at', 'operator': 'GTE', 'value': str(start_ms)},
            ]}],
            'properties': ['hs_meeting_title', 'booked_at'],
            'limit': 100,
        }
        if after: body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30).json()
        for m in r.get('results', []):
            yield m
        after = (r.get('paging') or {}).get('next', {}).get('after')
        if not after: break


def list_assoc(meeting_id, target):
    r = requests.get(
        f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/{target}',
        headers=HS, timeout=15).json()
    return [row['toObjectId'] for row in (r.get('results') or [])]


def primary_company(contact_id):
    r = requests.get(
        f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/companies',
        headers=HS, timeout=15).json()
    rows = r.get('results') or []
    return rows[0]['toObjectId'] if rows else None


def assoc_meeting_company(meeting_id, company_id):
    r = requests.put(
        f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/default/companies/{company_id}',
        headers=HS, timeout=15)
    return r.status_code


def main():
    scanned = updated = already_ok = skipped = 0
    for m in search_meetings():
        scanned += 1
        mid = m['id']
        existing_co = list_assoc(mid, 'companies')
        if existing_co:
            already_ok += 1
            continue
        contacts = list_assoc(mid, 'contacts')
        if not contacts:
            skipped += 1
            continue
        co_id = primary_company(contacts[0])
        if not co_id:
            skipped += 1
            continue
        sc = assoc_meeting_company(mid, co_id)
        if sc in (200, 201, 204):
            updated += 1
            title = (m.get('properties', {}).get('hs_meeting_title') or '')[:60]
            print(f'  ✓ {mid} -> company {co_id}   {title}')
        else:
            print(f'  ✗ {mid} -> company {co_id} (HTTP {sc})')
    print(f'\nscanned: {scanned}, updated: {updated}, already had company: {already_ok}, skipped (no contact/company): {skipped}')


if __name__ == '__main__':
    main()
