# scripts/patch_btc_2026_08_12.py
"""One-off: move meeting 390614433491 (The Best MGA / Dani) from conference_source
'other' to the Broker Tech Conference 2026 bucket, creating the bucket if missing.
Run once on Railway (has HS_API_KEY): `railway run python scripts/patch_btc_2026_08_12.py`."""
import requests
import meeting_bot as mb

MEETING_ID = '390614433491'

def main():
    resolved = mb.resolve_or_create_conference('BTC 2026', '2026-09-01')
    assert resolved, 'could not resolve BTC 2026'
    value = resolved['value']
    r = requests.patch(
        f'https://api.hubapi.com/crm/v3/objects/meetings/{MEETING_ID}',
        headers=mb.HS,
        json={'properties': {
            'conference_source': value,
            'hs_meeting_title': f'FurtherAI + The Best MGA [{value}]',
        }},
        timeout=30)
    print('patch', r.status_code, '->', value)
    assert r.status_code == 200, r.text

if __name__ == '__main__':
    main()
