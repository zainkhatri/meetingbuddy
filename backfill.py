"""One-shot backfill: re-scan #bdr-team for the last N days, tag/create
HubSpot meetings, and ❤️ each detected booking message so the user can
see in Slack which posts the bot would have caught.

Run via:
    railway run --service meetingbuddy python -u backfill.py [DAYS]
DAYS defaults to 7.
"""
import os
import sys
import time

import requests

import meeting_bot as mb

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7

SLK = {'Authorization': f'Bearer {mb.SLACK_BOT_TOKEN}'}


def react(channel, ts, name='heart'):
    try:
        requests.post('https://slack.com/api/reactions.add',
                      headers=SLK,
                      data={'channel': channel, 'timestamp': ts, 'name': name},
                      timeout=10)
    except Exception:
        pass


def main():
    rr = requests.get('https://slack.com/api/users.conversations',
                      headers=SLK,
                      params={'types': 'public_channel,private_channel', 'limit': 100},
                      timeout=15).json()
    channels = [c for c in (rr.get('channels') or []) if c.get('name') == 'bdr-team']
    if not channels:
        print('bdr-team not found in bot conversations'); return
    ch = channels[0]
    cid = ch['id']
    cutoff = str(time.time() - DAYS * 86400)
    print(f'scanning #{ch["name"]} since {DAYS}d ago (cutoff={cutoff})')

    msgs = []
    cursor = None
    while True:
        params = {'channel': cid, 'oldest': cutoff, 'limit': 200, 'inclusive': 'true'}
        if cursor: params['cursor'] = cursor
        r = requests.get('https://slack.com/api/conversations.history',
                         headers=SLK, params=params, timeout=20).json()
        msgs.extend(r.get('messages') or [])
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor: break
    print(f'  fetched {len(msgs)} messages')

    silent = lambda **kw: None
    booking_shaped = processed = 0
    for m in reversed(msgs):  # oldest first
        if m.get('bot_id') or m.get('subtype'): continue
        text = (m.get('text') or '').strip()
        if not text or not mb._looks_like_booking(text): continue
        booking_shaped += 1
        ts = m.get('ts'); user_id = m.get('user')
        if not ts or not user_id: continue
        try:
            parsed_raw = mb.parse_with_claude(text)
        except Exception as e:
            print(f'  parse error ts={ts}: {e}'); continue
        if not parsed_raw: continue
        bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
        bookings = [b for b in bookings if b and b.get('is_booking')]
        if not bookings: continue
        owner_id = mb.slack_user_to_owner(mb.app.client, user_id)
        any_ok = False
        for parsed in bookings:
            try:
                mb._process_booking(parsed, text, owner_id, ts, mb.app.client, silent)
                processed += 1
                any_ok = True
            except Exception as e:
                print(f'  process error ts={ts}: {e}')
        if any_ok:
            react(cid, ts)
            preview = text.replace('\n', ' ')[:80]
            print(f'  ❤️  {ts}  {preview}')

    print(f'\ndone — {booking_shaped} booking-shaped, {processed} processed')


if __name__ == '__main__':
    main()
