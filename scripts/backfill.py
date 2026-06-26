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
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def _fetch_once(cid):
    msgs, cursor = [], None
    while True:  # ponytail: Slack paginates; loop bounded by next_cursor
        # No `oldest`: for a freshly-joined channel Slack returns inconsistent
        # results when oldest is set. Pull the channel and filter by date below.
        params = {'channel': cid, 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        r = requests.get('https://slack.com/api/conversations.history',
                         headers=SLK, params=params, timeout=20).json()
        if not r.get('ok'):
            print(f'  slack error: {r.get("error")} (is the bot in this channel?)')
            return None
        msgs.extend(r.get('messages') or [])
        cursor = (r.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
    return msgs


def fetch_messages(cid, cutoff):
    # Retry on empty: a just-joined channel is eventually-consistent and
    # intermittently returns 0 messages even when history exists.
    msgs = []
    for attempt in range(5):
        msgs = _fetch_once(cid)
        if msgs is None:  # hard API error — don't retry
            return []
        if msgs:
            break
        print(f'  got 0 messages, retrying ({attempt + 1}/5; channel just-joined consistency)…')
        time.sleep(2)
    return [m for m in msgs if float(m.get('ts', 0)) >= cutoff]


def _patch(meeting_id, props):
    r = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{meeting_id}',
                       headers=mb.HS, json={'properties': props}, timeout=30)
    return r.status_code == 200


def _derive_type(parsed):
    """Demo vs conference from the post TEXT (the channel is mixed pre-2026-06-12, so
    the post header — 'DEMO BOOKED' vs '<Conf> MEETING BOOKED' — is the truth). Trust
    the parser's meeting_type header; only fall to conference_source when it's silent."""
    pt = parsed.get('meeting_type')
    if pt == 'demo':
        return 'demo'
    if pt == 'conference' or parsed.get('conference_source'):
        return 'conference'
    return 'demo'  # demos-booked is BDR prospect bookings; non-conference == demo


def reclassify(cid, msgs, execute):
    """Patch ONLY meeting_type (+conference_source/hs_activity_type) on the matched
    existing meeting, per the post text. No booked_at re-stamp, no Ellen-sheet push,
    no deal creation — unlike _process_booking. Never creates meetings."""
    matched = patched = nomatch = 0
    for m in reversed(msgs):  # oldest first
        if m.get('bot_id') or m.get('subtype'):
            continue
        text = (m.get('text') or '').strip()
        ts = m.get('ts')
        if not text or not ts or not mb._looks_like_booking(text):
            continue
        ref = datetime.fromtimestamp(float(ts), timezone.utc).strftime('%Y-%m-%d')
        try:
            parsed_raw = mb.parse_with_claude(text, ref)
        except Exception as e:
            print(f'  parse error ts={ts}: {e}'); continue
        bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
        for parsed in [b for b in bookings if b and b.get('is_booking')]:
            company = parsed.get('company_name')
            email = parsed.get('contact_email')
            date_str = parsed.get('meeting_date')
            label = f'{parsed.get("contact_first_name","")} {parsed.get("contact_last_name","")}'.strip() \
                or company or '?'
            mtype = _derive_type(parsed)
            try:  # one transient HubSpot timeout shouldn't abort the whole run
                contact = mb.hs_find_contact(parsed.get('contact_first_name'),
                                             parsed.get('contact_last_name'), email, company)
                existing = mb.hs_find_existing_meeting(contact['id'], date_str) if contact else None
                if existing and company and not email and \
                        not mb.attribution.title_matches_company(existing.get('title', ''), company):
                    existing = None
                if not existing:
                    existing = mb.hs_find_meeting_by_company_date(company, date_str)
            except Exception as e:
                print(f'  [error] {label}: {e}'); continue
            if not existing:
                nomatch += 1
                print(f'  [no-match] {mtype:10} | {label}')
                continue
            matched += 1
            props = {'meeting_type': mtype}
            if mtype == 'conference':
                props['hs_activity_type'] = 'Conference'
                conf = parsed.get('conference_source') or mb.detect_conference_from_title(existing.get('title') or '')
                if conf:
                    props['conference_source'] = conf
            tag = 'PATCH' if execute else 'would'
            print(f'  [{tag}] {mtype:10} -> {existing["id"]} | {label} | {existing.get("title","")[:38]}')
            if execute:
                try:
                    if _patch(existing['id'], props):
                        patched += 1
                except Exception as e:
                    print(f'  [patch-error] {existing["id"]}: {e}')
    print(f'\nreclassify done — matched={matched} patched={patched} no-match={nomatch}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', default='conference-meetings',
                    help='channel name alias (conference-meetings/demos-booked) or raw Slack id')
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--execute', action='store_true', help='write to HubSpot (default: dry-run)')
    ap.add_argument('--reclassify', action='store_true',
                    help='patch-only: set meeting_type from post text on the matched meeting '
                         '(for the mixed pre-6/12 demos-booked history). No create/sheet/deal side-effects.')
    args = ap.parse_args()

    cid = CHANNEL_ALIASES.get(args.channel, args.channel)
    profile = mb.CHANNEL_PROFILE.get(cid, {})
    cutoff = time.time() - args.days * 86400
    mode = 'EXECUTE' if args.execute else 'DRY-RUN'
    print(f'[{mode}] scanning {args.channel} ({cid}) since {args.days}d ago; '
          f'channel-type={profile.get("meeting_type") or "(text-inferred)"}')

    msgs = fetch_messages(cid, cutoff)
    print(f'  fetched {len(msgs)} messages')

    if args.reclassify:
        reclassify(cid, msgs, args.execute)
        return

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
