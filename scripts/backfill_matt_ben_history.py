#!/usr/bin/env python3
"""One-time history backfill for Matt Stapleton + Ben Trotter.

They were only added to the bot's sourcing roster on 2026-06-01, and the live
bot never re-scans Slack history older than 24h. So every booking they posted
before then was processed while they weren't recognised -> the meeting got no
`meeting_sourced_by` and no owner, so it never showed under their name.

This walks the FULL history of every channel the bot is in, finds the bookings
*posted by* Matt or Ben, matches each to the existing HubSpot meeting (using the
bot's own matching logic, copied verbatim), and tags it. It only TAGS EXISTING
meetings — it never creates contacts or meetings, and never touches Ellen's
sheet. It is idempotent and refuses to overwrite a meeting already credited to a
different owner (those are reported as conflicts for manual review).

Standalone: depends only on `requests`. Tokens come from the environment
(SLACK_BOT_TOKEN, ANTHROPIC_API_KEY, HS_API_KEY) — the same vars the bot uses.

    python3 scripts/backfill_matt_ben_history.py            # dry run
    python3 scripts/backfill_matt_ben_history.py --execute   # write
    python3 scripts/backfill_matt_ben_history.py --since 2026-04-01
"""
import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests

SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
HS_API_KEY = os.environ['HS_API_KEY']

SLK = {'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
HS = {'Authorization': f'Bearer {HS_API_KEY}', 'Content-Type': 'application/json'}

# Only these two. Last-name substring match against the poster's Slack real_name,
# exactly as the bot resolves them (NAME_TO_OWNER). 'ben'/'matt' are unsafe
# substrings; 'trotter'/'stapleton' are unambiguous.
NAME_TO_OWNER = {
    'trotter': '164943105',   # Ben Trotter
    'stapleton': '92184259',  # Matt Stapleton
}
OWNER_NAME = {'164943105': 'Ben Trotter', '92184259': 'Matt Stapleton'}

PARSE_MODEL = 'claude-haiku-4-5-20251001'

# --- Parser prompt (verbatim from meeting_bot.PARSE_PROMPT) ---
PARSE_PROMPT = """You parse Slack meeting announcements from a sales team. Extract structured data.

The team books meetings with insurance industry prospects. Two common formats:

FORMAT A (free-form):
"Meeting booked! Contact: Jane Doe, COO. Company: Acme Insurance. Demo on 4/30 at 10am. Source: LinkedIn."

FORMAT B (5-field structured, sometimes with a header like "TARGET MARKETS MEETING!" or "DEMO!" or "CONFERENCE MEETING!"):
"TARGET MARKETS MEETING!
Keith Steen – Director, Marine Operations @ Compass Marine Programs
Austin Devnew – Program Director, CMIP @ Compass Marine Programs
Thursday, April 30th, 10:00 AM CDT
Source: Email
Location: Table #46"

Both are valid bookings — set is_booking=true for either.

MULTIPLE BOOKINGS in one message: if the post announces N distinct meetings (different contact AND/OR different company, e.g. "Two booth meetings confirmed: 1. ... 2. ..."), return a JSON ARRAY of N booking objects, one per meeting. If it's a single meeting (even with co-attendees from the same company), return a single object.

Format B header signals the meeting context: "TARGET MARKETS" / "TMPAA" / "TMPCC" → conference_source=tmpaa (all Target Markets / TMPAA events are one bucket); "DEMO" → meeting_type=demo; bare "MEETING!" with no conference → infer from source_channel.

Return ONLY valid JSON (no prose). If a field is not mentioned, use null.

Schema:
{
  "is_booking": boolean,
  "contact_first_name": string|null,
  "contact_last_name": string|null,
  "contact_title": string|null,
  "contact_email": string|null,
  "contact_linkedin": string|null,
  "company_name": string|null,
  "meeting_type": "intro"|"demo"|"scoping"|"discovery"|"followup"|"checkin"|"conference"|null,
  "source_channel": "email"|"linkedin"|"referral"|"call"|"conference"|"inbound"|null,
  "conference_source": "wsia_uw_summit"|"wsia_dinner"|"insurtech_ny_spring"|"insurtech_insights"|"insurance_innovators"|"tmpaa"|"rims_riskworld"|"nashville_dinner"|"ny_dinner"|"insurance_insider"|"reuters_es"|"reuters_program_managers"|"other"|null,
  "meeting_date": "YYYY-MM-DD"|null,
  "meeting_time_utc": "HH:MM"|null,
  "location": string|null,
  "notes": string|null
}

`location` is the physical or virtual where (booth, table number, room, address, Zoom). It does NOT belong in conference_source.

conference_source rules:
  - Set ONLY from the EVENT named in the header or Source line ("INSURTECH INSIGHTS MEETING!", "Source: Brella (Insurtech Insights)", "RIMS RISKWORLD 2026", "Insurance Innovators", "MFLive (Insurance Innovators Nashville)", etc.).
  - DO NOT infer conference_source from words in the COMPANY NAME. A company called "Greater New York Insurance Companies", "InsurTech NY Holdings", "Nashville Brokers", or "Rims Solutions Inc" tells you nothing about which conference the meeting belongs to — only the explicit event tag does.
  - If the post has no explicit conference header AND no conference in Source → conference_source=null.
  - Synonyms: "IIUSA" / "Insurance Innovators USA" → conference_source=insurance_innovators (same event).
  - Synonyms: "TMPAA" / "TMPCC" / "Target Markets" / "Target Markets Mid-Year" / "Target Markets Annual" → conference_source=tmpaa (same org, one bucket).
  - Synonyms: "Reuters E&S" / "E&S Reuters Conference" / "Reuters - The Insurer E&S" / "Reuters The Insurer E&S" / "E&S Insurer" → conference_source=reuters_es.
  - Synonyms: "Reuters Program Managers" / "Program Managers Conference" / "Reuters - The Insurer Program Manager" / "The Insurer Program Manager" → conference_source=reuters_program_managers.

source_channel mapping:
  - "Source: Email" / cold email phrasing → "email"
  - "Source: LinkedIn" / LinkedIn URL or DM mentioned as the channel → "linkedin"
  - "Source: Call" / "Source: Phone" → "call"
  - "Source: Referral" / "intro from X" → "referral"
  - "Source: Inbound" / "they reached out" → "inbound"
  - Any conference-platform source ("Brella", "RIMS RISKWORLD", "Insurtech Insights", "Target Markets", "WSIA", "Insurance Insider", booth/table mentions as the source, header like "TARGET MARKETS MEETING" / "RIMS MEETING" / "INSURTECH INSIGHTS MEETING") → "conference"
  - When in doubt and a conference is involved → "conference"

Set is_booking=true AGGRESSIVELY. ANY of these signals → is_booking=true:
  - Headers like "Demo Booked", "Meeting Booked", "BOOKED!", "DEMO!", "Demo Booked!!", "*Booked!*", "Conference Meeting", "Target Markets Meeting", "ITNY Meeting", "WSIA Meeting", "RIMS Meeting" — even with markdown bold/asterisks
  - A prospect name + company + date/time mentioned together
  - Any post listing a person, title, company, and a meeting time
  - "Demo with X", "Call with X", "Meeting with X" + a future date
  - Slack bold/italic wrappers (asterisks, underscores) DO NOT change meaning — strip them mentally

Set is_booking=false ONLY for: general chat, questions, internal team coordination, asking about availability, status updates that don't announce a new booking, FYIs that don't book a meeting.

When in doubt, set is_booking=true. False negatives are worse than false positives here.

Message:
"""


def parse_with_claude(text, reference_date):
    prompt = (PARSE_PROMPT
              + f"\n[Reference date — the message was posted on {reference_date}. "
                f"If the message gives a date without a year, assume the year that makes "
                f"the meeting fall AFTER the reference date (typically the same year, or "
                f"next year if the month has already passed). NEVER default to past years.]\n\n"
              + text)
    try:
        r = requests.post('https://api.anthropic.com/v1/messages',
                          headers={'x-api-key': ANTHROPIC_API_KEY,
                                   'anthropic-version': '2023-06-01',
                                   'content-type': 'application/json'},
                          json={'model': PARSE_MODEL, 'max_tokens': 1024,
                                'messages': [{'role': 'user', 'content': prompt}]},
                          timeout=60)
        if r.status_code != 200:
            print(f'  [claude {r.status_code}] {r.text[:120]}', flush=True)
            return None
        txt = r.json()['content'][0]['text'].strip()
        txt = re.sub(r'^```json\s*', '', txt)
        txt = re.sub(r'\s*```$', '', txt)
        return json.loads(txt)
    except Exception as e:
        print(f'  [claude parse error] {e}', flush=True)
        return None


def looks_like_booking(text):
    t = (text or '').lower()
    return any(kw in t for kw in ('meeting', 'demo', 'booked', 'call with', 'intro'))


# --- conference-from-title (verbatim from meeting_bot._CONF_RULES) ---
_CONF_RULES = [
    (r'\binsurtech\s+insights\b', 'insurtech_insights'),
    (r'\binsurance\s+innovators\b', 'insurance_innovators'),
    (r'\binsurance\s+insider\b', 'insurance_insider'),
    (r'\bwsia\s+dinner\b', 'wsia_dinner'),
    (r'\bwsia[\s_]uw[\s_]summit\b', 'wsia_uw_summit'),
    (r'\bwsia\b', 'wsia_uw_summit'),
    (r'\binsurtech[\s_]?ny[\s_]?(spring)?\b', 'insurtech_ny_spring'),
    (r'\bitny\d*\b', 'insurtech_ny_spring'),
    (r'\btmpaa\b', 'tmpaa'),
    (r'\btmpcc\b', 'tmpcc'),
    (r'target[\s_]markets', 'tmpaa'),
    (r'\btm[\s_](connect|meeting)\b', 'tmpaa'),
    (r'\brims[\s_]?(riskworld)?\b', 'rims_riskworld'),
    (r'\briskworld\b', 'rims_riskworld'),
    (r'\bnashville\s+dinner\b', 'nashville_dinner'),
    (r'\b(new\s+york|ny)\s+dinner\b', 'ny_dinner'),
    (r'\biiusa\b', 'iiusa'),
]


def detect_conference_from_title(title):
    t = (title or '').lower()
    for pat, val in _CONF_RULES:
        if re.search(pat, t):
            return val
    return None


# --- HubSpot matching (verbatim from meeting_bot) ---
def hs_find_contact(first, last, email=None):
    filters = []
    if email:
        filters = [{'propertyName': 'email', 'operator': 'EQ', 'value': email.lower()}]
    else:
        if first:
            filters.append({'propertyName': 'firstname', 'operator': 'EQ', 'value': first})
        if last:
            filters.append({'propertyName': 'lastname', 'operator': 'EQ', 'value': last})
    if not filters:
        return None
    body = {'filterGroups': [{'filters': filters}], 'properties': ['firstname', 'lastname', 'email'], 'limit': 1}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts/search', headers=HS, json=body, timeout=30)
    rs = r.json().get('results', [])
    return rs[0] if rs else None


def hs_find_existing_meeting(contact_id, date_str):
    if not contact_id:
        return None
    r = requests.get(f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/meetings',
                     headers=HS, timeout=15)
    if r.status_code != 200:
        return None
    target = None
    if date_str:
        try:
            target = datetime.fromisoformat(f'{date_str}T12:00:00+00:00')
        except Exception:
            target = None
    candidates = []
    for a in r.json().get('results', []):
        mid = str(a['toObjectId'])
        rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}', headers=HS,
                          params={'properties': 'hs_meeting_start_time,meeting_sourced_by,hs_meeting_outcome,hs_meeting_title,hubspot_owner_id'},
                          timeout=10)
        if rg.status_code != 200:
            continue
        p = rg.json().get('properties', {})
        if (p.get('hs_meeting_outcome') or '') in ('CANCELED', 'NO_SHOW'):
            continue
        if (p.get('hs_meeting_title') or '').lower().startswith('canceled:'):
            continue
        start = p.get('hs_meeting_start_time')
        if not start:
            continue
        try:
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except Exception:
            continue
        diff = abs((start_dt - target).total_seconds()) if target else 1e12
        candidates.append((diff, mid, p.get('meeting_sourced_by', ''),
                           p.get('hubspot_owner_id', ''), p.get('hs_meeting_title', '')))
    if not candidates:
        return None
    candidates.sort()
    diff, mid, sourced_by, existing_owner, title = candidates[0]
    if target and diff > 5 * 86400:
        return None
    return {'id': mid, 'sourced_by': sourced_by, 'title': title, 'owner_id': existing_owner}


def hs_find_meeting_by_company_date(company_name, date_str):
    if not company_name or not date_str:
        return None
    try:
        target = datetime.fromisoformat(f'{date_str}T12:00:00+00:00')
    except Exception:
        return None
    lo_ms = int((target.timestamp() - 5 * 86400) * 1000)
    hi_ms = int((target.timestamp() + 5 * 86400) * 1000)
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_meeting_title', 'operator': 'CONTAINS_TOKEN', 'value': company_name},
            {'propertyName': 'hs_meeting_start_time', 'operator': 'BETWEEN', 'value': str(lo_ms), 'highValue': str(hi_ms)},
        ]}],
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by', 'hs_meeting_outcome', 'hubspot_owner_id'],
        'limit': 20,
    }
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search', headers=HS, json=body, timeout=30)
    if r.status_code != 200:
        return None
    for m in r.json().get('results', []):
        p = m.get('properties') or {}
        if (p.get('hs_meeting_outcome') or '') in ('CANCELED', 'NO_SHOW'):
            continue
        if (p.get('hs_meeting_title') or '').lower().startswith('canceled:'):
            continue
        return {'id': m['id'], 'sourced_by': p.get('meeting_sourced_by', ''),
                'title': p.get('hs_meeting_title', ''),
                'owner_id': p.get('hubspot_owner_id', '')}
    return None


# --- Slack ---
def slack_get(method, params):
    for i in range(5):
        r = requests.get(f'https://slack.com/api/{method}', headers=SLK, params=params, timeout=30)
        d = r.json()
        if d.get('error') == 'ratelimited':
            time.sleep(int(r.headers.get('Retry-After', 2)))
            continue
        return d
    return {}


def list_channels():
    d = slack_get('users.conversations', {'types': 'public_channel,private_channel', 'limit': 200})
    return d.get('channels', []) or []


def channel_replies(cid, parent_ts):
    """All replies under a thread parent (conversations.history omits these)."""
    out, cursor = [], None
    for _ in range(50):  # fixed bound
        params = {'channel': cid, 'ts': parent_ts, 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        d = slack_get('conversations.replies', params)
        if not d.get('ok'):
            break
        out.extend(d.get('messages', []) or [])
        cursor = (d.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
        time.sleep(0.3)
    return out


def channel_history(cid):
    """Full channel history, oldest-first, INCLUDING thread replies — Slack's
    conversations.history returns only parents + broadcasts, so we expand every
    thread via conversations.replies. Dedup by ts."""
    parents, cursor = [], None
    for _ in range(200):  # fixed bound: 200 pages * 200 = 40k msgs max
        params = {'channel': cid, 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        d = slack_get('conversations.history', params)
        if not d.get('ok'):
            print(f'  [warn] history error on {cid}: {d.get("error")}', flush=True)
            break
        parents.extend(d.get('messages', []) or [])
        cursor = (d.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
        time.sleep(0.4)
    seen, out = set(), []
    for m in parents:
        if m.get('ts') and m['ts'] not in seen:
            seen.add(m['ts'])
            out.append(m)
        if m.get('reply_count'):
            for r in channel_replies(cid, m['ts']):
                if r.get('ts') and r['ts'] not in seen:
                    seen.add(r['ts'])
                    out.append(r)
    out.sort(key=lambda m: float(m.get('ts', 0)))  # oldest first
    return out


_uname_cache = {}


def owner_for_user(uid):
    """Resolve a Slack user to Matt/Ben's owner id, or None. Last-name substring
    match on real_name — identical to the bot's slack_user_to_owner logic."""
    assert uid, 'owner_for_user requires a user id'
    if uid in _uname_cache:
        return _uname_cache[uid]
    d = slack_get('users.info', {'user': uid})
    info = d.get('user') or {}
    display = (info.get('real_name') or info.get('name') or '').lower()
    result = None
    for key, oid in NAME_TO_OWNER.items():
        if key in display:
            result = oid
            break
    _uname_cache[uid] = result
    return result


def post_date(ts):
    assert ts, 'post_date requires a ts'
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime('%Y-%m-%d')


def find_existing_meeting(parsed):
    contact = hs_find_contact(parsed.get('contact_first_name'),
                              parsed.get('contact_last_name'),
                              parsed.get('contact_email'))
    contact_id = contact['id'] if contact else None
    date_str = parsed.get('meeting_date')
    existing = hs_find_existing_meeting(contact_id, date_str) if contact_id else None
    if not existing:
        existing = hs_find_meeting_by_company_date(parsed.get('company_name'), date_str)
    return existing


def build_props(parsed, owner_id, ts, existing):
    booked_ms = int(float(ts) * 1000)
    props = {
        'meeting_sourced_by': owner_id,
        'booked_at': str(booked_ms),
        'hs_timestamp': str(booked_ms),
    }
    if parsed.get('meeting_type'):
        props['meeting_type'] = parsed['meeting_type']
        if parsed['meeting_type'] == 'conference':
            props['hs_activity_type'] = 'Conference'
    if parsed.get('source_channel'):
        props['meeting_source_channel'] = parsed['source_channel']
    conf = parsed.get('conference_source') or detect_conference_from_title(existing.get('title') or '')
    if conf:
        props['conference_source'] = conf
    if not existing.get('owner_id') and owner_id:
        props['hubspot_owner_id'] = owner_id  # claim only if currently unowned
    return props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true', help='write to HubSpot (default: dry run)')
    ap.add_argument('--since', help='only messages on/after YYYY-MM-DD')
    ap.add_argument('--limit', type=int, default=0, help='cap bookings written (testing)')
    args = ap.parse_args()

    since_ts = 0.0
    if args.since:
        since_ts = datetime.fromisoformat(args.since + 'T00:00:00+00:00').timestamp()

    mode = 'EXECUTE' if args.execute else 'DRY RUN'
    print(f'=== Matt + Ben history backfill [{mode}] ===', flush=True)

    channels = list_channels()
    print(f'Bot is in {len(channels)} channel(s): {[c.get("name") for c in channels]}', flush=True)

    counts = Counter()
    unmatched, conflicts, tagged_rows, correct_rows = [], [], [], []

    for ch in channels:
        cid, cname = ch.get('id'), ch.get('name')
        if not cid:
            continue
        msgs = channel_history(cid)
        print(f'\n#{cname}: {len(msgs)} messages', flush=True)
        for m in msgs:
            if m.get('bot_id') or m.get('subtype'):
                continue
            ts, uid = m.get('ts'), m.get('user')
            text = (m.get('text') or '').strip()
            if not ts or not uid or not text:
                continue
            if float(ts) < since_ts:
                continue
            owner_id = owner_for_user(uid)
            if not owner_id:
                continue  # not Matt or Ben
            # No keyword prefilter — parse every message from a roster author so
            # nothing a BDR posted can slip past (thoroughness over Claude cost).
            counts['msgs_from_roster'] += 1

            parsed_raw = parse_with_claude(text, post_date(ts))
            if not parsed_raw:
                counts['parse_fail'] += 1
                continue
            bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
            for parsed in bookings:
                if not parsed or not parsed.get('is_booking'):
                    continue
                counts['bookings'] += 1
                who = OWNER_NAME[owner_id]
                label = f'{parsed.get("contact_first_name") or ""} {parsed.get("contact_last_name") or ""}'.strip()
                co = parsed.get('company_name') or '?'

                existing = find_existing_meeting(parsed)
                if not existing:
                    counts['no_meeting_match'] += 1
                    unmatched.append((who, post_date(ts), label, co, parsed.get('meeting_date')))
                    continue

                cur = (existing.get('sourced_by') or '').strip()
                if cur == owner_id:
                    counts['already_correct'] += 1
                    correct_rows.append((who, existing['id'], label, co, parsed.get('meeting_date')))
                    continue
                if cur and cur != owner_id:
                    counts['conflict'] += 1
                    conflicts.append((who, existing['id'], cur, label, co, parsed.get('meeting_date')))
                    continue

                props = build_props(parsed, owner_id, ts, existing)
                claimed_owner = bool(props.get('hubspot_owner_id'))
                if args.execute:
                    r = requests.patch(
                        f'https://api.hubapi.com/crm/v3/objects/meetings/{existing["id"]}',
                        headers=HS, json={'properties': props}, timeout=30)
                    if r.status_code == 200:
                        counts['tagged'] += 1
                    else:
                        counts['patch_fail'] += 1
                        print(f'  [patch fail {r.status_code}] meeting {existing["id"]}: {r.text[:120]}', flush=True)
                        continue
                else:
                    counts['would_tag'] += 1
                tagged_rows.append((who, existing['id'], label, co, parsed.get('meeting_date'), claimed_owner))

                if args.limit and (counts['tagged'] + counts['would_tag']) >= args.limit:
                    print('\n[limit reached]', flush=True)
                    _report(counts, tagged_rows, unmatched, conflicts, correct_rows, mode)
                    return

    _report(counts, tagged_rows, unmatched, conflicts, correct_rows, mode)


def _report(counts, tagged_rows, unmatched, conflicts, correct_rows, mode):
    print(f'\n===== RESULT [{mode}] =====', flush=True)
    print(dict(counts), flush=True)

    if correct_rows:
        print(f'\nALREADY CREDITED ({len(correct_rows)}):', flush=True)
        for who, mid, label, co, mdate in correct_rows:
            print(f'  {who:<14} meeting={mid} {label} @ {co} ({mdate})', flush=True)

    verb = 'TAGGED' if mode == 'EXECUTE' else 'WOULD TAG'
    print(f'\n{verb} ({len(tagged_rows)}):', flush=True)
    for who, mid, label, co, mdate, claimed in tagged_rows:
        own = ' +owner' if claimed else ''
        print(f'  {who:<14} meeting={mid} {label} @ {co} ({mdate}){own}', flush=True)

    if conflicts:
        print(f'\nCONFLICTS — matched meeting already credited to another owner ({len(conflicts)}):', flush=True)
        for who, mid, cur, label, co, mdate in conflicts:
            print(f'  {who:<14} meeting={mid} sourced_by={cur} {label} @ {co} ({mdate})', flush=True)

    if unmatched:
        print(f'\nNO MEETING MATCH — booking posted but no HubSpot meeting found ({len(unmatched)}):', flush=True)
        for who, pdate, label, co, mdate in unmatched:
            print(f'  {who:<14} posted={pdate} {label} @ {co} (meeting_date={mdate})', flush=True)


if __name__ == '__main__':
    main()
