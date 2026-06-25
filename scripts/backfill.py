"""One-shot backfill: re-scan a booking channel for the last N days, tag/create
HubSpot meetings using the SAME channel-driven typing as the live bot, and ❤️
each detected booking so the user can see in Slack which posts were caught.

The channel is the authoritative meeting-type signal (demos-booked -> demo,
conference-meetings -> conference). We pass channel= into _process_booking so a
conference-channel backfill stamps meeting_type=conference + conference_source +
hs_activity_type=Conference, exactly like a live post would.

Dry-run by default (prints what it WOULD do); pass --execute to write to HubSpot.

Run via:
    railway run --service meetingbuddy python -u scripts/backfill.py \
        --channel conference-meetings --days 120 [--execute]
"""
import argparse
import time

import requests

import meeting_bot as mb

SLK = {'Authorization': f'Bearer {mb.SLACK_BOT_TOKEN}'}

# Friendly names -> the bot's authoritative channel-id constants.
CHANNEL_ALIASES = {
    'conference-meetings': mb.CONFERENCE_MEETINGS_CHANNEL,
    'conference': mb.CONFERENCE_MEETINGS_CHANNEL,
    'demos-booked': mb.DEMOS_BOOKED_CHANNEL,
    'demos': mb.DEMOS_BOOKED_CHANNEL,
}


def react(channel, ts, name='heart'):
    try:
        requests.post('https://slack.com/api/reactions.add',
                      headers=SLK,
                      data={'channel': channel, 'timestamp': ts, 'name': name},
                      timeout=10)
    except Exception:
        pass


def fetch_messages(cid, cutoff):
    msgs, cursor = [], None
    while True:  # ponytail: Slack paginates; loop bounded by next_cursor
        params = {'channel': cid, 'oldest': cutoff, 'limit': 200, 'inclusive': 'true'}
        if cursor:
            params['cursor'] = cursor
        r = requests.get('https://slack.com/api/conversations.history',
                         headers=SLK, params=params, timeout=20).json()
        if not r.get('ok'):
            print(f'  slack error: {r.get("error")} (is the bot in this channel?)')
            break
        msgs.extend(r.get('messages') or [])
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', default='conference-meetings',
                    help='channel name alias (conference-meetings/demos-booked) or raw Slack id')
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--execute', action='store_true', help='write to HubSpot (default: dry-run)')
    args = ap.parse_args()

    cid = CHANNEL_ALIASES.get(args.channel, args.channel)
    profile = mb.CHANNEL_PROFILE.get(cid, {})
    cutoff = str(time.time() - args.days * 86400)
    mode = 'EXECUTE' if args.execute else 'DRY-RUN'
    print(f'[{mode}] scanning {args.channel} ({cid}) since {args.days}d ago; '
          f'channel-type={profile.get("meeting_type") or "(text-inferred)"}')

    msgs = fetch_messages(cid, cutoff)
    print(f'  fetched {len(msgs)} messages')

    silent = lambda **kw: None
    booking_shaped = processed = 0
    for m in reversed(msgs):  # oldest first
        if m.get('bot_id') or m.get('subtype'):
            continue
        text = (m.get('text') or '').strip()
        if not text or not mb._looks_like_booking(text):
            continue
        booking_shaped += 1
        ts = m.get('ts'); user_id = m.get('user')
        if not ts or not user_id:
            continue
        try:
            parsed_raw = mb.parse_with_claude(text)
        except Exception as e:
            print(f'  parse error ts={ts}: {e}'); continue
        if not parsed_raw:
            continue
        bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
        bookings = [b for b in bookings if b and b.get('is_booking')]
        if not bookings:
            continue
        owner_id = mb.slack_user_to_owner(mb.app.client, user_id)
        preview = text.replace('\n', ' ')[:80]
        any_ok = False
        for parsed in bookings:
            mtype = profile.get('meeting_type') or parsed.get('meeting_type')
            label = f'{parsed.get("contact_first_name","")} {parsed.get("contact_last_name","")}'.strip() \
                or parsed.get('company_name') or '?'
            if not args.execute:
                print(f'  [would] {ts} owner={owner_id} type={mtype} '
                      f'conf={parsed.get("conference_source")} src={parsed.get("source_channel")} | {label}')
                processed += 1
                any_ok = True
                continue
            try:
                mb._process_booking(parsed, text, owner_id, ts, mb.app.client, silent, channel=cid)
                processed += 1
                any_ok = True
            except Exception as e:
                print(f'  process error ts={ts}: {e}')
        if any_ok and args.execute:
            react(cid, ts)
            print(f'  ❤️  {ts}  {preview}')

    print(f'\ndone [{mode}] — {booking_shaped} booking-shaped, {processed} '
          f'{"would be " if not args.execute else ""}processed')


if __name__ == '__main__':
    main()
