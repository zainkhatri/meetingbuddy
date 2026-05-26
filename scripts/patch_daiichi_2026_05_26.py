"""One-shot: re-tag the Daiichi meeting that was orphaned by the fuzzy
reconciler on 2026-05-26. Zain's sourced_by/booked_at landed on meeting
371732228806 → merged into 371769615089 → merged into 371770213090,
where the BDR tag was dropped.

Run once after deploying the merge-winner fix.
"""
import os, sys, requests

HS_API_KEY = os.environ['HS_API_KEY']
HS = {'Authorization': f'Bearer {HS_API_KEY}', 'Content-Type': 'application/json'}

MEETING_ID = '371770213090'
ZAIN_OWNER_ID = '88760040'
SLACK_TS = '1779807929.344449'   # Zain's Daiichi post, 2026-05-26 11:05 ET
BOOKED_MS = str(int(float(SLACK_TS) * 1000))

props = {
    'meeting_sourced_by': ZAIN_OWNER_ID,
    'booked_at': BOOKED_MS,
    'hs_timestamp': BOOKED_MS,
    'meeting_type': 'conference',
    'hs_activity_type': 'Conference',
    'meeting_source_channel': 'conference',
    'conference_source': 'insurtech_insights',
}

r = requests.patch(
    f'https://api.hubapi.com/crm/v3/objects/meetings/{MEETING_ID}',
    headers=HS, json={'properties': props}, timeout=30,
)
print(r.status_code, r.text[:300])
sys.exit(0 if r.status_code == 200 else 1)
