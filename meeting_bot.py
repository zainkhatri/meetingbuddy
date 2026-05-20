#!/usr/bin/env python3
"""Slack → Claude → HubSpot meeting bot.

Listens in #bdr-gang. When a BDR posts a booking announcement, Claude parses it
and the bot creates/updates HubSpot records (contact, company, meeting) and
replies in-thread with a confirmation.

Ground rules BDRs should follow (flexible — Claude handles variance):
  Meeting booked!
  Contact: <name>, <title>
  Company: <company>
  Meeting type: Demo / Intro / Scoping / Conference
  Source: Email / LinkedIn / Conference / Referral / Inbound
  Conference: <WSIA / ITNY / RIMS / Target Markets / ...>  (only if source = conference)
  Date: <M/D at time>
  LinkedIn: <url>  (optional)
  Notes: <...>  (optional)

Run:
  python3 meeting_bot.py

Runs Slack Socket Mode — no public URL needed.
"""
import json
import os
import re
import threading
import time
import requests
from datetime import datetime, timezone, timedelta

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import sheet_sync


# --- Credentials (all from env; fail fast if missing) ---
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
SLACK_APP_TOKEN = os.environ['SLACK_APP_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
HS_API_KEY = os.environ['HS_API_KEY']
HS = {'Authorization': f'Bearer {HS_API_KEY}', 'Content-Type': 'application/json'}

# --- Slack user → HubSpot owner mapping ---
SLACK_USER_TO_HS_OWNER = {
    # Fill these in after one-time lookup (users.list). Map by slack user_id.
    # Real-time bot will auto-populate on first message from a user.
}
# Fallback by display-name substring (case-insensitive)
# REAL BDR roster: Zain, Jacob, Dani only. Everyone else (Aman, Bobby, Mike,
# Nia, Gavin, Kush, Logan, etc.) is an AE/rep/teammate — they may attend or
# react but they do NOT source meetings for the dashboard.
NAME_TO_OWNER = {
    'zain': '88760040',
    'jacob': '162210484',
    'dani': '82377567',
    'daniella': '82377567',
}

# --- Claude parser ---
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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


def parse_with_claude(text, reference_date=None):
    if not client:
        return None
    ref = reference_date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    prompt = (PARSE_PROMPT
              + f"\n[Reference date — the message was posted on {ref}. "
                f"If the message gives a date without a year, assume the year that makes "
                f"the meeting fall AFTER the reference date (typically the same year, or "
                f"next year if the month has already passed). NEVER default to past years.]\n\n"
              + text)
    try:
        r = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': prompt}],
        )
        txt = r.content[0].text.strip()
        # Strip markdown code fences if present
        txt = re.sub(r'^```json\s*', '', txt)
        txt = re.sub(r'\s*```$', '', txt)
        return json.loads(txt)
    except Exception as e:
        print(f'Claude parse error: {e}')
        return None


# --- HubSpot helpers ---
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


def hs_find_company(name):
    if not name:
        return None
    body = {'filterGroups': [{'filters': [{'propertyName': 'name', 'operator': 'EQ', 'value': name}]}],
            'properties': ['name', 'hubspot_owner_id'], 'limit': 1}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/companies/search', headers=HS, json=body, timeout=30)
    rs = r.json().get('results', [])
    return rs[0] if rs else None


def hs_create_contact(first, last, title, company_name, email=None, linkedin=None, owner_id=None):
    props = {}
    if first: props['firstname'] = first
    if last: props['lastname'] = last
    if title: props['jobtitle'] = title
    if email: props['email'] = email.lower()
    if linkedin: props['hs_linkedin_url'] = linkedin
    if company_name: props['company'] = company_name
    if owner_id: props['hubspot_owner_id'] = owner_id
    body = {'properties': props}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts', headers=HS, json=body, timeout=30)
    return r.json() if r.status_code in (200, 201) else None


def hs_associate_contact_company(contact_id, company_id):
    r = requests.put(
        f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/companies/{company_id}',
        headers=HS,
        json=[{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 279}],
        timeout=30)
    return r.status_code in (200, 201)


_CONF_RULES = [
    # More-specific patterns first.
    (r'\binsurtech\s+insights\b',         'insurtech_insights'),
    (r'\binsurance\s+innovators\b',       'insurance_innovators'),
    (r'\binsurance\s+insider\b',          'insurance_insider'),
    (r'\bwsia\s+dinner\b',                'wsia_dinner'),
    (r'\bwsia[\s_]uw[\s_]summit\b',       'wsia_uw_summit'),
    (r'\bwsia\b',                         'wsia_uw_summit'),
    (r'\binsurtech[\s_]?ny[\s_]?(spring)?\b', 'insurtech_ny_spring'),
    (r'\bitny\d*\b',                      'insurtech_ny_spring'),
    (r'\btmpaa\b',                        'tmpaa'),
    (r'\btmpcc\b',                        'tmpcc'),
    (r'target[\s_]markets',               'tmpaa'),
    (r'\btm[\s_](connect|meeting)\b',     'tmpaa'),
    (r'\brims[\s_]?(riskworld)?\b',       'rims_riskworld'),
    (r'\briskworld\b',                    'rims_riskworld'),
    (r'\bnashville\s+dinner\b',           'nashville_dinner'),
    (r'\b(new\s+york|ny)\s+dinner\b',     'ny_dinner'),
    (r'\biiusa\b',                        'iiusa'),
]

def detect_conference_from_title(title):
    t = (title or '').lower()
    for pat, val in _CONF_RULES:
        if re.search(pat, t):
            return val
    return None


# Conference date windows (start_date, end_date_inclusive, conf_value).
# Used as fallback when title has no marker and Claude didn't classify.
_CONF_DATE_WINDOWS = [
    ('2026-03-22', '2026-03-25', 'wsia_uw_summit'),
    ('2026-03-30', '2026-04-01', 'insurtech_ny_spring'),
    ('2026-04-22', '2026-04-24', 'insurance_insider'),
    ('2026-04-27', '2026-04-30', 'tmpaa'),
    ('2026-05-03', '2026-05-07', 'rims_riskworld'),
    ('2026-05-11', '2026-05-12', 'insurance_innovators'),  # Music City Center, Nashville
    ('2026-06-03', '2026-06-04', 'insurtech_insights'),    # New York
]

def detect_conference_from_date(date_str):
    if not date_str:
        return None
    d = (date_str or '')[:10]
    for lo, hi, val in _CONF_DATE_WINDOWS:
        if lo <= d <= hi:
            return val
    return None


def hs_find_meeting_by_company_date(company_name, date_str):
    """Fallback when no contact match: search meetings whose title contains the company
    name and whose start_time is within ±5 days of the announced date.
    Catches GCal-synced meetings before the contact has been created/associated."""
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


def hs_find_existing_meeting(contact_id, date_str):
    """Find the contact's meeting whose start_time best matches the announced date.
    Uses date proximity (±5 days) — handles contacts with multiple meetings correctly."""
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
        candidates.append((diff, mid, p.get('meeting_sourced_by', ''), p.get('hubspot_owner_id', '')))

    if not candidates:
        return None
    candidates.sort()  # closest match first
    diff, mid, sourced_by, existing_owner = candidates[0]
    # If we have a target, only accept matches within ±5 days
    if target and diff > 5 * 86400:
        return None
    # Re-fetch title for caller (used for conference auto-tag)
    rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}', headers=HS,
                      params={'properties': 'hs_meeting_title'}, timeout=10)
    title = (rg.json().get('properties') or {}).get('hs_meeting_title', '') if rg.status_code == 200 else ''
    return {'id': mid, 'sourced_by': sourced_by, 'title': title, 'owner_id': existing_owner}


def hs_update_meeting(meeting_id, sourced_by, mtype=None, channel=None, conf=None):
    props = {}
    if sourced_by: props['meeting_sourced_by'] = sourced_by
    if mtype: props['meeting_type'] = mtype
    if channel: props['meeting_source_channel'] = channel
    if conf: props['conference_source'] = conf
    # Mirror conference tag to the native HubSpot "Call and meeting type" field
    # (hs_activity_type) so it shows up on the meeting record card in the UI.
    if mtype == 'conference': props['hs_activity_type'] = 'Conference'
    if not props:
        return False
    r = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{meeting_id}',
                       headers=HS, json={'properties': props}, timeout=30)
    return r.status_code == 200


def hs_create_meeting(title, date_str, time_str, contact_id, sourced_by, meeting_type, source_channel,
                     conference_source, notes, owner_id=None, company_id=None):
    # Build start time
    if date_str and time_str:
        try:
            start = datetime.fromisoformat(f'{date_str}T{time_str}:00+00:00')
        except Exception:
            start = datetime.now(timezone.utc)
    elif date_str:
        start = datetime.fromisoformat(f'{date_str}T14:00:00+00:00')
    else:
        start = datetime.now(timezone.utc)
    end = start.replace(microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = start_ms + 30 * 60 * 1000
    props = {
        'hs_timestamp': str(start_ms),
        'hs_meeting_title': title,
        'hs_meeting_start_time': str(start_ms),
        'hs_meeting_end_time': str(end_ms),
        'hs_meeting_outcome': 'SCHEDULED',
        'hs_meeting_body': notes or '',
    }
    if sourced_by: props['meeting_sourced_by'] = sourced_by
    if meeting_type: props['meeting_type'] = meeting_type
    if source_channel: props['meeting_source_channel'] = source_channel
    if conference_source: props['conference_source'] = conference_source
    if meeting_type == 'conference': props['hs_activity_type'] = 'Conference'
    if owner_id: props['hubspot_owner_id'] = owner_id
    body = {'properties': props}
    assocs = []
    if contact_id:
        assocs.append({'to': {'id': contact_id},
                       'types': [{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 200}]})
    if company_id:
        # meeting -> company HUBSPOT_DEFINED association type id is 188
        assocs.append({'to': {'id': company_id},
                       'types': [{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 188}]})
    if assocs:
        body['associations'] = assocs
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings', headers=HS, json=body, timeout=30)
    return r.json() if r.status_code in (200, 201) else None


# --- Slack bot ---
app = App(token=SLACK_BOT_TOKEN)


def slack_user_to_owner(slack_client, slack_user_id):
    """Look up Slack user's real name and map to HubSpot owner id."""
    if slack_user_id in SLACK_USER_TO_HS_OWNER:
        return SLACK_USER_TO_HS_OWNER[slack_user_id]
    try:
        info = slack_client.users_info(user=slack_user_id).get('user') or {}
        display = (info.get('real_name') or info.get('name') or '').lower()
        for key, oid in NAME_TO_OWNER.items():
            if key in display:
                SLACK_USER_TO_HS_OWNER[slack_user_id] = oid
                return oid
    except Exception:
        pass
    return None


# In-memory set of Slack ts values this process has already begun processing.
# Prevents the live handler and the live_sweep thread from racing on the
# same message. The HubSpot booked_at dedup is the long-term cross-restart
# protection; this set covers within-process races.
PROCESSED_TS = set()
PROCESSED_TS_LOCK = threading.Lock()


def _claim_ts(ts):
    """Returns True if this caller is the first to claim `ts`. False otherwise."""
    with PROCESSED_TS_LOCK:
        if ts in PROCESSED_TS:
            return False
        PROCESSED_TS.add(ts)
        # Keep the set bounded — cap at 5000 oldest entries
        if len(PROCESSED_TS) > 5000:
            PROCESSED_TS.clear()
            PROCESSED_TS.add(ts)
        return True


@app.event('message')
def handle_message(event, client, say, logger):
    # Bot messages: skip
    if event.get('bot_id'):
        return
    subtype = event.get('subtype')
    # Edits: extract the new message and reprocess. Downstream HubSpot lookups
    # are idempotent (find-or-create contact, find-or-tag existing meeting), so
    # re-running on an edit either no-ops or fills in details that were missing
    # on the original post.
    if subtype == 'message_changed':
        msg = event.get('message') or {}
        if msg.get('bot_id'):
            return
        text = (msg.get('text') or '').strip()
        user_id = msg.get('user')
        ts = msg.get('ts')
    elif subtype:
        # Other subtypes (channel_join, message_deleted, etc.) — skip
        return
    else:
        text = (event.get('text') or '').strip()
        user_id = event.get('user')
        ts = event.get('ts')
    if not text or not ts:
        return
    if not _claim_ts(ts):
        print(f'[live] ts={ts} already claimed (sweep beat us) — skipping')
        return

    # Parse — Claude may return a single dict or a list of dicts (multi-booking post)
    parsed_raw = parse_with_claude(text)
    if not parsed_raw:
        return
    bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
    bookings = [b for b in bookings if b and b.get('is_booking')]
    if not bookings:
        return

    owner_id = slack_user_to_owner(client, user_id)
    channel = event.get('channel')
    print(f'[live] handle_message ts={ts} channel={channel} bookings={len(bookings)}')
    try:
        client.reactions_add(channel=channel, timestamp=ts, name='heart')
    except Exception as e:
        print(f'[live] reactions_add failed: {e}')
    for parsed in bookings:
        _process_booking(parsed, text, owner_id, ts, client, say)


def _push_to_ellen_sheet(*, conference_slug, owner_id, meeting_date, meeting_time_utc,
                          existing_start_ms, company_name, first, last, title, email, outcome):
    """Best-effort upsert into Ellen's Full Meeting Tracker. Returns suffix for Slack reply."""
    if not conference_slug or not company_name:
        return ''
    # Compute start time ms: prefer the one already on the existing meeting; else
    # build from parsed date/time the same way hs_create_meeting does.
    start_ms = existing_start_ms
    if not start_ms and meeting_date:
        try:
            if meeting_time_utc:
                dt = datetime.fromisoformat(f'{meeting_date}T{meeting_time_utc}:00+00:00')
            else:
                dt = datetime.fromisoformat(f'{meeting_date}T14:00:00+00:00')
            start_ms = int(dt.timestamp() * 1000)
        except Exception:
            start_ms = None
    try:
        payload = sheet_sync.build_payload(
            conference_slug=conference_slug,
            sourced_by_owner_id=owner_id,
            meeting_start_ms=start_ms,
            company=company_name,
            contact_first=first,
            contact_last=last,
            contact_title=title,
            contact_email=email,
            hs_meeting_outcome=outcome,
        )
        result = sheet_sync.upsert_meeting_row(payload)
        action = result.get('action')
        if action in ('inserted', 'updated'):
            return f" + sheet ({action})"
        return ''
    except Exception:
        print('[sheet_sync] unexpected error', flush=True)
        return ''


def _process_booking(parsed, text, owner_id, ts, client, say):
    company_name = parsed.get('company_name')
    first = parsed.get('contact_first_name')
    last = parsed.get('contact_last_name')
    email = parsed.get('contact_email')

    # If a conference is involved, default source_channel to "conference" when
    # Claude couldn't classify it (e.g. "Source: Brella" doesn't match the basic enum).
    if not parsed.get('source_channel') and parsed.get('conference_source'):
        parsed['source_channel'] = 'conference'
    # And default meeting_type to "conference" for conference-sourced meetings
    # unless Claude already classified as something more specific (demo/intro/etc.).
    if not parsed.get('meeting_type') and parsed.get('conference_source'):
        parsed['meeting_type'] = 'conference'

    # Fold location into notes (no dedicated meeting_location property)
    loc = parsed.get('location')
    notes_combined = parsed.get('notes') or ''
    if loc:
        prefix = f'Location: {loc}'
        notes_combined = f'{prefix}\n{notes_combined}'.strip() if notes_combined else prefix
    parsed['notes'] = notes_combined or None

    # Guard: a "booking" with no contact AND no company is unattachable —
    # dedup needs a contact_id, so re-runs would create duplicate skeleton
    # meetings titled after the location. Skip it.
    if not (first or last or email) and not company_name:
        print(f'[skip] booking-shaped but no contact or company: {text[:80]!r}')
        return

    # 1. Find or create company
    co = hs_find_company(company_name) if company_name else None
    company_id = co['id'] if co else None

    # 2. Find or create contact
    contact = hs_find_contact(first, last, email)
    if not contact and (first or last or email):
        contact = hs_create_contact(first, last, parsed.get('contact_title'),
                                     company_name, email, parsed.get('contact_linkedin'), owner_id)
    contact_id = contact['id'] if contact else None

    if contact_id and company_id:
        hs_associate_contact_company(contact_id, company_id)

    # 3. Date-aware dedup — find the existing meeting closest to the announced date and tag it
    date_str = parsed.get('meeting_date')
    existing = hs_find_existing_meeting(contact_id, date_str) if contact_id else None
    # Fallback: GCal-synced meeting may exist before the contact is associated.
    # Search by company name + date window.
    if not existing:
        existing = hs_find_meeting_by_company_date(company_name, date_str)
    if existing:
        # Tag with sourced_by + booked_at (Slack post timestamp) + type/channel/conference
        booked_ms = int(float(ts) * 1000)
        update_props = {
            'meeting_sourced_by': owner_id,
            'booked_at': str(booked_ms),
        }
        if parsed.get('meeting_type'):
            update_props['meeting_type'] = parsed['meeting_type']
            if parsed['meeting_type'] == 'conference':
                update_props['hs_activity_type'] = 'Conference'
        if parsed.get('source_channel'):
            update_props['meeting_source_channel'] = parsed['source_channel']
        # Conference: prefer Claude's call, fall back to title pattern on the EXISTING meeting
        conf = parsed.get('conference_source') or detect_conference_from_title(existing.get('title') or '')
        if conf:
            update_props['conference_source'] = conf
        # Assign meeting owner to the sourcer if it's currently unowned, so it
        # counts in the "Meetings Booked per BDR" report (which groups by owner).
        if not existing.get('owner_id') and owner_id:
            update_props['hubspot_owner_id'] = owner_id
        # hs_timestamp = "Activity date" in HubSpot UI; set to Slack announce time
        # so reports show when the meeting was booked, not when GCal first synced it.
        update_props['hs_timestamp'] = str(booked_ms)
        # Ensure existing meeting has both contact + company associations so
        # HubSpot reports surface who/what the meeting is with.
        if contact_id:
            try:
                requests.put(
                    f'https://api.hubapi.com/crm/v4/objects/meetings/{existing["id"]}/associations/default/contacts/{contact_id}',
                    headers=HS, timeout=15)
            except Exception: pass
        if company_id:
            try:
                requests.put(
                    f'https://api.hubapi.com/crm/v4/objects/meetings/{existing["id"]}/associations/default/companies/{company_id}',
                    headers=HS, timeout=15)
            except Exception: pass
        r_patch = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{existing["id"]}',
                                  headers=HS, json={'properties': update_props}, timeout=30)
        if r_patch.status_code != 200:
            say(text=f'⚠️ Failed to update meeting: {r_patch.status_code}', thread_ts=ts)
            return
        # Enqueue for re-patching in case GCal re-syncs and clobbers booked_at
        enqueue_retry(existing['id'], update_props)
        # Push to Ellen's sheet (best-effort)
        sheet_result = _push_to_ellen_sheet(
            conference_slug=conf,
            owner_id=owner_id,
            meeting_date=date_str,
            meeting_time_utc=parsed.get('meeting_time_utc'),
            existing_start_ms=existing.get('start_time_ms'),
            company_name=company_name,
            first=first, last=last,
            title=parsed.get('contact_title'),
            email=email,
            outcome='SCHEDULED',
        )
        portal_id = '44712408'
        mtg_url = f"https://app-na2.hubspot.com/contacts/{portal_id}/record/0-47/{existing['id']}"
        prev = existing['sourced_by']
        action = 'Re-tagged existing meeting' if prev and prev != owner_id else 'Tagged existing meeting'
        say(text=f"✓ {action} (was {prev or 'untagged'}){sheet_result}\n{mtg_url}", thread_ts=ts)
        return

    # 4. Create meeting
    mtg_title = f"FurtherAI + {company_name}" if company_name else (parsed.get('notes') or 'Meeting')
    if parsed.get('meeting_type') == 'demo':
        mtg_title += ' [Demo]'
    elif parsed.get('conference_source'):
        mtg_title += f" [{parsed['conference_source']}]"

    # Fallbacks: title pattern → date window
    if not parsed.get('conference_source'):
        parsed['conference_source'] = (
            detect_conference_from_title(mtg_title)
            or detect_conference_from_date(parsed.get('meeting_date'))
        )

    mtg = hs_create_meeting(
        title=mtg_title,
        date_str=parsed.get('meeting_date'),
        time_str=parsed.get('meeting_time_utc'),
        contact_id=contact_id,
        sourced_by=owner_id,
        meeting_type=parsed.get('meeting_type'),
        source_channel=parsed.get('source_channel'),
        conference_source=parsed.get('conference_source'),
        notes=parsed.get('notes'),
        owner_id=owner_id,
        company_id=company_id,
    )

    # 4. Stamp booked_at + hs_timestamp = Slack post timestamp on the new meeting.
    # hs_timestamp is HubSpot's "Activity date" — overrides the start-time default
    # so reports show when the BDR booked it.
    if mtg and mtg.get('id'):
        booked_ms = int(float(ts) * 1000)
        stamp_props = {'booked_at': str(booked_ms), 'hs_timestamp': str(booked_ms)}
        requests.patch(f"https://api.hubapi.com/crm/v3/objects/meetings/{mtg['id']}",
                       headers=HS, json={'properties': stamp_props},
                       timeout=30)
        # Bot-created meetings shouldn't get clobbered, but enqueue defensively —
        # also lets the retry loop re-apply tags if anything resets them.
        full_props = dict(stamp_props)
        if owner_id: full_props['meeting_sourced_by'] = owner_id
        if parsed.get('meeting_type'):
            full_props['meeting_type'] = parsed['meeting_type']
            if parsed['meeting_type'] == 'conference':
                full_props['hs_activity_type'] = 'Conference'
        if parsed.get('source_channel'): full_props['meeting_source_channel'] = parsed['source_channel']
        if parsed.get('conference_source'): full_props['conference_source'] = parsed['conference_source']
        enqueue_retry(mtg['id'], full_props)

    # 5. Push to Ellen's sheet (best-effort)
    sheet_result = ''
    if mtg and mtg.get('id'):
        sheet_result = _push_to_ellen_sheet(
            conference_slug=parsed.get('conference_source'),
            owner_id=owner_id,
            meeting_date=parsed.get('meeting_date'),
            meeting_time_utc=parsed.get('meeting_time_utc'),
            existing_start_ms=None,
            company_name=company_name,
            first=first, last=last,
            title=parsed.get('contact_title'),
            email=email,
            outcome='SCHEDULED',
        )

    # 6. Reply
    if mtg and mtg.get('id'):
        portal_id = '44712408'
        mtg_url = f"https://app-na2.hubspot.com/contacts/{portal_id}/record/0-47/{mtg['id']}"
        pieces = []
        if parsed.get('meeting_type'): pieces.append(parsed['meeting_type'])
        if parsed.get('source_channel'): pieces.append(f"via {parsed['source_channel']}")
        if parsed.get('conference_source'): pieces.append(f"@ {parsed['conference_source']}")
        tag = ' · '.join(pieces) if pieces else 'meeting'
        confirmation = (
            f"✓ Logged {tag}{sheet_result}\n"
            f"Contact: {first or ''} {last or ''} ({parsed.get('contact_title') or '—'}) @ {company_name or '—'}\n"
            f"{mtg_url}"
        )
        say(text=confirmation, thread_ts=ts)
    else:
        say(text="⚠️ I parsed your message but couldn't create the HubSpot meeting. Check my logs.", thread_ts=ts)


# --- Retry queue: re-apply tags for 24h to catch GCal clobbers ---
# When the bot tags or creates a meeting, we record the intended property
# values. A worker re-checks every 5 min: if any property has been cleared
# (e.g. GCal re-synced and wiped booked_at), patch it back. Drops entries
# after 24h.
_retry_queue = []  # list of {'meeting_id', 'props', 'first_seen', 'attempts'}
_retry_lock = threading.Lock()
RETRY_TTL_SEC = 7 * 24 * 3600

def enqueue_retry(meeting_id, props):
    if not meeting_id or not props:
        return
    with _retry_lock:
        # Replace any existing entry for the same meeting with merged props
        for entry in _retry_queue:
            if entry['meeting_id'] == meeting_id:
                entry['props'].update({k: v for k, v in props.items() if v})
                return
        _retry_queue.append({
            'meeting_id': str(meeting_id),
            'props': {k: v for k, v in props.items() if v},
            'first_seen': time.time(),
            'attempts': 0,
        })

def retry_pass():
    now = time.time()
    with _retry_lock:
        # Drop expired
        _retry_queue[:] = [e for e in _retry_queue if now - e['first_seen'] < RETRY_TTL_SEC]
        snapshot = list(_retry_queue)
    repaired = 0
    for entry in snapshot:
        mid = entry['meeting_id']
        props = entry['props']
        try:
            r = requests.get(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}',
                             headers=HS, params={'properties': ','.join(props.keys())},
                             timeout=15)
            if r.status_code != 200:
                continue
            current = (r.json().get('properties') or {})
            # Detect drift: any intended prop missing or different
            drift = {}
            for k, want in props.items():
                have = current.get(k)
                if not have:
                    drift[k] = want
                    continue
                # Normalize epoch-ms vs ISO comparison for date fields
                if k in ('booked_at', 'hs_timestamp'):
                    try:
                        have_ms = int(datetime.fromisoformat(have.replace('Z', '+00:00')).timestamp() * 1000)
                        want_ms = int(want)
                        if abs(have_ms - want_ms) > 60000:  # >1min off
                            drift[k] = want
                    except Exception:
                        if str(have) != str(want):
                            drift[k] = want
                elif str(have).lower() != str(want).lower():
                    drift[k] = want
            if drift:
                rp = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}',
                                    headers=HS, json={'properties': drift}, timeout=30)
                if rp.status_code == 200:
                    repaired += 1
                    print(f'[retry] re-patched {mid}: {list(drift.keys())}')
            entry['attempts'] += 1
        except Exception as e:
            print(f'[retry] error on {mid}: {e}')
    return repaired


def retry_loop():
    while True:
        try:
            n = retry_pass()
            if n:
                print(f'[retry] repaired {n} meeting(s)')
        except Exception as e:
            print(f'[retry] loop error: {e}')
        time.sleep(300)


# --- Startup replay: re-process the last 24h of Slack history ---
# Catches messages posted while the bot was down (Railway restart, deploy,
# socket disconnect). HubSpot lookups are idempotent — already-tagged
# meetings stay correctly tagged, untagged ones get tagged.
def _looks_like_booking(text):
    t = (text or '').lower()
    return any(kw in t for kw in ('meeting', 'demo', 'booked', 'call with', 'intro'))

SLK = {'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}


def replay_missed_messages():
    time.sleep(5)  # let the socket connect first
    try:
        rr = requests.get('https://slack.com/api/users.conversations',
                          headers=SLK,
                          params={'types': 'public_channel,private_channel', 'limit': 100},
                          timeout=15).json()
        channels = rr.get('channels', []) or []
    except Exception as e:
        print(f'[replay] could not list channels: {e}')
        return
    print(f'[replay] found {len(channels)} channel(s): {[c.get("name") for c in channels]}')
    # Slack's conversations.history rejects float-formatted `oldest` (e.g.
    # "1778530757.15") and silently returns msgs=0. Must be int seconds.
    cutoff = str(int(time.time() - 24 * 3600))
    silent_say = lambda **kw: None
    processed = 0
    for ch in channels:
        cid = ch.get('id')
        if not cid:
            continue
        try:
            rr = requests.get('https://slack.com/api/conversations.history',
                              headers=SLK,
                              params={'channel': cid, 'oldest': cutoff, 'limit': 200, 'inclusive': 'true'},
                              timeout=20).json()
            msgs = rr.get('messages', []) or []
            print(f'[replay] {ch.get("name")}: ok={rr.get("ok")} error={rr.get("error")} '
                  f'msgs={len(msgs)} cutoff={cutoff}')
        except Exception as e:
            print(f'[replay] history error on {cid}: {e}')
            continue
        kept = 0
        for m in reversed(msgs):  # oldest first
            if m.get('bot_id') or m.get('subtype'):
                continue
            text = (m.get('text') or '').strip()
            if not text or not _looks_like_booking(text):
                continue
            kept += 1
            ts = m.get('ts')
            user_id = m.get('user')
            if not ts or not user_id:
                continue
            try:
                parsed_raw = parse_with_claude(text)
            except Exception:
                continue
            if not parsed_raw:
                continue
            bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
            bookings = [b for b in bookings if b and b.get('is_booking')]
            if not bookings:
                continue
            owner_id = slack_user_to_owner(app.client, user_id)
            # Replay-dedup guard: if any HubSpot meeting already carries this
            # Slack post's booked_at, the post was processed on a prior run.
            # Re-running risks Claude normalizing a name differently and
            # creating a parallel record the reconciler then has to merge
            # (e.g. "Franklin Maddison" vs "Franklin Madison").
            booked_ms = int(float(ts) * 1000)
            try:
                dup_search = requests.post(
                    'https://api.hubapi.com/crm/v3/objects/meetings/search',
                    headers=HS,
                    json={'filterGroups': [{'filters': [
                        {'propertyName': 'booked_at', 'operator': 'EQ', 'value': str(booked_ms)},
                    ]}], 'properties': ['hs_meeting_title'], 'limit': 1},
                    timeout=15)
                if dup_search.status_code == 200 and dup_search.json().get('total', 0) > 0:
                    print(f'[replay] skip ts={ts}: booked_at already tagged on an existing meeting')
                    # Recover the heart in case the original processor crashed
                    # after tagging HubSpot but before reacting (or reactions_add
                    # failed silently). Slack returns already_reacted harmlessly.
                    try:
                        requests.post('https://slack.com/api/reactions.add', headers=SLK,
                                      data={'channel': cid, 'timestamp': ts, 'name': 'heart'},
                                      timeout=10)
                    except Exception:
                        pass
                    continue
            except Exception as e:
                print(f'[replay] dedup check failed ts={ts}: {e} — falling through')
            any_ok = False
            for parsed in bookings:
                try:
                    _process_booking(parsed, text, owner_id, ts, app.client, silent_say)
                    processed += 1
                    any_ok = True
                except Exception as e:
                    print(f'[replay] process error ts={ts}: {e}')
            if any_ok:
                try:
                    app.client.reactions_add(channel=cid, timestamp=ts, name='heart')
                except Exception:
                    pass
        print(f'[replay] {ch.get("name")}: {kept} booking-shaped, {processed} processed (cumulative)')
    print(f'[replay] done — re-processed {processed} booking(s) from last 24h')


# --- Reconciler: merge bot-created/GCal twin pairs ---
# Race: bot fires on Slack post → no GCal meeting yet → bot creates one. Later
# GCal syncs the real calendar event, leaving a duplicate untagged copy.
# This sweep finds those pairs (same owner + same day, one has booked_at, other
# doesn't, one's title starts with "FurtherAI + ") and merges metadata onto
# the GCal copy, then deletes the bot-created duplicate.
def reconcile_duplicates():
    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp() * 1000)
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_createdate', 'operator': 'GTE', 'value': str(since_ms)},
        ]}],
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by',
                       'booked_at', 'hubspot_owner_id', 'meeting_type',
                       'meeting_source_channel', 'conference_source', 'hs_timestamp'],
        'limit': 100,
    }
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                      headers=HS, json=body, timeout=30)
    if r.status_code != 200:
        return 0
    by_key = {}
    for m in r.json().get('results', []):
        p = m.get('properties') or {}
        oid = p.get('hubspot_owner_id')
        st = p.get('hs_meeting_start_time')
        if not oid or not st:
            continue
        try:
            start_dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
        except Exception:
            continue
        by_key.setdefault((oid, start_dt.date().isoformat()), []).append((m, start_dt, p))

    merged = 0
    for items in by_key.values():
        if len(items) < 2:
            continue
        # Find bot-created (has booked_at + title prefix) and GCal twin (no booked_at, ±2h)
        for bot_m, bot_dt, bot_p in items:
            if not bot_p.get('booked_at'):
                continue
            if not (bot_p.get('hs_meeting_title') or '').startswith('FurtherAI + '):
                continue
            for gcal_m, gcal_dt, gcal_p in items:
                if gcal_m['id'] == bot_m['id']:
                    continue
                if gcal_p.get('booked_at'):
                    continue
                if abs((bot_dt - gcal_dt).total_seconds()) > 7200:
                    continue
                # Merge bot metadata onto GCal copy
                merge_props = {}
                for k in ('booked_at', 'meeting_sourced_by', 'meeting_source_channel',
                          'conference_source', 'meeting_type', 'hs_timestamp'):
                    v = bot_p.get(k)
                    if v and not gcal_p.get(k):
                        merge_props[k] = v
                if merge_props.get('meeting_type') == 'conference':
                    merge_props['hs_activity_type'] = 'Conference'
                if merge_props:
                    requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{gcal_m["id"]}',
                                   headers=HS, json={'properties': merge_props}, timeout=30)
                # Delete bot-created duplicate
                requests.delete(f'https://api.hubapi.com/crm/v3/objects/meetings/{bot_m["id"]}',
                                headers=HS, timeout=30)
                print(f'[reconcile] merged {bot_m["id"]} → {gcal_m["id"]}, deleted dup')
                merged += 1
                break

    # Second pass: same owner + same exact start_time, fuzzy-similar company name.
    # Catches the case where the bot couldn't dedup against an existing GCal
    # meeting (e.g. spelling diff: "Franklin Maddison" vs "Franklin Madison")
    # so it created a parallel record. Both end up with booked_at set, which the
    # first pass skips. Here we pair by exact start-time + name similarity.
    def _norm_company(title):
        if not title: return ''
        # Strip "FurtherAI + " prefix, "[conf]" brackets, "(conf)" parens
        t = re.sub(r'^FurtherAI\s*\+\s*', '', title)
        t = re.sub(r'\s*\[[^\]]*\]\s*', '', t)
        t = re.sub(r'\s*\([^)]*\)\s*', '', t)
        return re.sub(r'[^a-z0-9]', '', t.lower())
    def _edit_distance(a, b, cap=3):
        # Bail out early if length diff > cap
        if abs(len(a) - len(b)) > cap: return cap + 1
        if a == b: return 0
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i] + [0] * len(b)
            for j, cb in enumerate(b, 1):
                cur[j] = min(cur[j-1]+1, prev[j]+1, prev[j-1] + (ca != cb))
            if min(cur) > cap: return cap + 1
            prev = cur
        return prev[-1]

    # Re-group by exact start_time (regardless of booked_at)
    by_start = {}
    for m in r.json().get('results', []):
        p = m.get('properties') or {}
        oid = p.get('hubspot_owner_id'); st = p.get('hs_meeting_start_time')
        if not oid or not st: continue
        by_start.setdefault((oid, st), []).append((m, p))
    for items in by_start.values():
        if len(items) < 2: continue
        # Pair any two whose normalized company names match with edit distance ≤ 2
        used = set()
        for i, (m_a, p_a) in enumerate(items):
            if m_a['id'] in used: continue
            comp_a = _norm_company(p_a.get('hs_meeting_title'))
            if not comp_a: continue
            for m_b, p_b in items[i+1:]:
                if m_b['id'] in used: continue
                comp_b = _norm_company(p_b.get('hs_meeting_title'))
                if not comp_b: continue
                if _edit_distance(comp_a, comp_b, cap=2) > 2: continue
                # Winner: the one WITHOUT a [bracket] tag in title (= GCal copy)
                has_bracket_a = '[' in (p_a.get('hs_meeting_title') or '')
                has_bracket_b = '[' in (p_b.get('hs_meeting_title') or '')
                if has_bracket_a and not has_bracket_b:
                    winner, loser, wp, lp = m_b, m_a, p_b, p_a
                else:
                    winner, loser, wp, lp = m_a, m_b, p_a, p_b
                # Copy missing fields from loser
                merge = {}
                for k in ('booked_at', 'meeting_sourced_by', 'meeting_source_channel',
                          'conference_source', 'meeting_type', 'hs_timestamp'):
                    if lp.get(k) and not wp.get(k):
                        merge[k] = lp[k]
                if merge.get('meeting_type') == 'conference':
                    merge['hs_activity_type'] = 'Conference'
                if merge:
                    requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{winner["id"]}',
                                   headers=HS, json={'properties': merge}, timeout=30)
                requests.delete(f'https://api.hubapi.com/crm/v3/objects/meetings/{loser["id"]}',
                                headers=HS, timeout=30)
                print(f'[reconcile-fuzzy] merged {loser["id"]} → {winner["id"]}, '
                      f'companies "{comp_a}" vs "{comp_b}"')
                used.add(loser['id']); used.add(winner['id'])
                merged += 1
                break
    return merged


# Sweep: find recently-created GCal meetings with no sourced_by, look up their
# associated contact's other (bot-created) meetings to copy metadata. Catches
# the case where the bot tagged a meeting but the patch didn't stick OR where
# the bot never saw the GCal meeting at all because it synced after the post.
def sweep_untagged_gcal():
    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=12)).timestamp() * 1000)
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_createdate', 'operator': 'GTE', 'value': str(since_ms)},
            {'propertyName': 'meeting_sourced_by', 'operator': 'NOT_HAS_PROPERTY'},
        ]}],
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'hubspot_owner_id'],
        'limit': 50,
    }
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                      headers=HS, json=body, timeout=30)
    if r.status_code != 200:
        return 0
    # Untagged GCal meetings — leave them alone unless reconcile_duplicates picks them up.
    # (We only auto-tag when there's a bot-created twin to copy from; otherwise we'd be
    # guessing the BDR who sourced it.)
    return len(r.json().get('results', []))


def reconcile_loop():
    while True:
        try:
            n = reconcile_duplicates()
            if n:
                print(f'[reconcile] merged {n} duplicate pair(s)')
        except Exception as e:
            print(f'[reconcile] error: {e}')
        time.sleep(300)


def sheet_reconcile_loop():
    """Run scripts/sheet_reconcile.py once a day (24h), backfilling Apollo
    enrichment for any meeting the inline write missed phones/emails on.
    Sleeps 60s on boot to let everything else settle first."""
    import subprocess
    time.sleep(60)
    while True:
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts', 'sheet_reconcile.py')
            r = subprocess.run(['python3', script, '--since', '14'], capture_output=True, timeout=900)
            print(f'[sheet-reconcile] exit={r.returncode}')
            for line in (r.stdout.decode('utf-8', 'replace').splitlines()[-5:]
                         + r.stderr.decode('utf-8', 'replace').splitlines()[-3:]):
                print(f'[sheet-reconcile] {line}')
        except Exception as e:
            print(f'[sheet-reconcile] error: {e}')
        time.sleep(24 * 3600)


def live_sweep_loop():
    """Backup to the live Socket Mode handler: every 30 seconds, look at the
    last ~120s of #bdr-team and process any booking-shaped message we
    haven't already claimed in-process. If the websocket ever misses an
    event, this catches it within at most ~30s.

    Dedup: in-memory PROCESSED_TS prevents racing with the live handler;
    HubSpot booked_at search prevents racing across restarts.
    """
    print('[sweep] live sweep loop started (every 30s, 120s window)')
    while True:
        time.sleep(30)
        try:
            # Find #bdr-team channel id
            chans = requests.get('https://slack.com/api/users.conversations',
                                 headers=SLK,
                                 params={'types': 'public_channel,private_channel', 'limit': 200},
                                 timeout=15).json().get('channels', []) or []
            for ch in chans:
                cid = ch.get('id')
                if not cid:
                    continue
                oldest = str(int(time.time() - 120))
                rr = requests.get('https://slack.com/api/conversations.history',
                                  headers=SLK,
                                  params={'channel': cid, 'oldest': oldest, 'limit': 50, 'inclusive': 'true'},
                                  timeout=15).json()
                if not rr.get('ok'):
                    continue
                for m in reversed(rr.get('messages') or []):
                    if m.get('bot_id') or m.get('subtype'):
                        continue
                    text = (m.get('text') or '').strip()
                    ts = m.get('ts')
                    user_id = m.get('user')
                    if not text or not ts or not user_id:
                        continue
                    if not _looks_like_booking(text):
                        continue
                    if not _claim_ts(ts):
                        continue  # live handler beat us, or already processed this tick
                    # HubSpot dedup: if booked_at already tagged, skip and just heart
                    booked_ms = int(float(ts) * 1000)
                    try:
                        dup = requests.post(
                            'https://api.hubapi.com/crm/v3/objects/meetings/search',
                            headers=HS,
                            json={'filterGroups': [{'filters': [
                                {'propertyName': 'booked_at', 'operator': 'EQ', 'value': str(booked_ms)},
                            ]}], 'properties': ['hs_meeting_title'], 'limit': 1},
                            timeout=10)
                        already_tagged = dup.status_code == 200 and dup.json().get('total', 0) > 0
                    except Exception:
                        already_tagged = False
                    # Always try the heart; Slack returns already_reacted harmlessly
                    try:
                        requests.post('https://slack.com/api/reactions.add', headers=SLK,
                                      data={'channel': cid, 'timestamp': ts, 'name': 'heart'},
                                      timeout=10)
                    except Exception:
                        pass
                    if already_tagged:
                        continue
                    print(f'[sweep] catching missed booking ts={ts}')
                    try:
                        parsed_raw = parse_with_claude(text)
                    except Exception as e:
                        print(f'[sweep] parse error ts={ts}: {e}')
                        continue
                    if not parsed_raw:
                        continue
                    bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
                    bookings = [b for b in bookings if b and b.get('is_booking')]
                    if not bookings:
                        continue
                    owner_id = slack_user_to_owner(app.client, user_id)
                    silent_say = lambda **kw: None
                    for parsed in bookings:
                        try:
                            _process_booking(parsed, text, owner_id, ts, app.client, silent_say)
                        except Exception as e:
                            print(f'[sweep] process error ts={ts}: {e}')
        except Exception as e:
            print(f'[sweep] outer error: {e}')


LAST_EVENT_AT = time.time()


@app.middleware
def _track_last_event(next, body):
    # Update on every inbound Slack event (message, reaction, hello, etc.)
    global LAST_EVENT_AT
    LAST_EVENT_AT = time.time()
    next()


def periodic_restart(interval_seconds=1800):
    # Unconditional restart every 30 min. Most reliable defense against
    # any future "socket up, events stopped" failure mode we haven't yet
    # observed. Startup replay + booked_at dedup make recycling safe.
    # Uses exit(0) so Railway's ON_FAILURE restart cap isn't burned.
    time.sleep(interval_seconds)
    print(f'[periodic-restart] {interval_seconds}s elapsed — exiting for clean restart')
    os._exit(0)


# NOTE: removed event_flow_watchdog. It killed the bot every ~10 min because
# the bot only sees events from channels it's a member of (#bdr-team), and
# that channel is regularly quiet for longer than 10 min. Constant restarts
# made the bot feel non-autonomous. Rely on socket_watchdog +
# slack_rest_watchdog + periodic_restart instead.


def slack_rest_watchdog():
    # Final layer: verify the Slack REST API is reachable from this
    # container every 5 min via auth.test. A failure here means the
    # token, network, or Slack itself is broken — exit so Railway can
    # try a fresh container.
    while True:
        time.sleep(300)
        try:
            r = requests.get('https://slack.com/api/auth.test',
                             headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'},
                             timeout=15)
            ok = r.status_code == 200 and r.json().get('ok')
        except Exception as e:
            print(f'[rest-watchdog] auth.test errored: {e}')
            ok = False
        if not ok:
            print('[rest-watchdog] Slack REST unreachable — exiting for clean restart')
            os._exit(0)


def socket_watchdog(handler, max_disconnected_seconds=120):
    # Exit the process if Socket Mode reports disconnected for too long, so
    # Railway's restart policy can recycle the container. Without this the
    # process can stay "alive" with a dead websocket and silently miss messages.
    disconnected_since = None
    while True:
        time.sleep(30)
        try:
            connected = handler.client is not None and handler.client.is_connected()
        except Exception as e:
            print(f'[watchdog] is_connected check failed: {e}')
            connected = False
        now = time.time()
        if connected:
            if disconnected_since is not None:
                print(f'[watchdog] socket reconnected after {int(now - disconnected_since)}s')
            disconnected_since = None
            continue
        if disconnected_since is None:
            disconnected_since = now
            print('[watchdog] socket reported disconnected')
            continue
        elapsed = now - disconnected_since
        if elapsed >= max_disconnected_seconds:
            print(f'[watchdog] socket dead for {int(elapsed)}s — exiting so Railway restarts')
            os._exit(1)


if __name__ == '__main__':
    print('Meeting Bot starting (Socket Mode)...')
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=sheet_reconcile_loop, daemon=True).start()
    print('[reconcile] background sweep started (every 5 min)')
    threading.Thread(target=retry_loop, daemon=True).start()
    print('[retry] background re-tag worker started (every 5 min, 24h TTL)')
    threading.Thread(target=replay_missed_messages, daemon=True).start()
    print('[replay] startup replay scheduled (last 24h)')
    threading.Thread(target=live_sweep_loop, daemon=True).start()
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    threading.Thread(target=socket_watchdog, args=(handler,), daemon=True).start()
    print('[watchdog] socket health watchdog started (30s checks, 120s tolerance)')
    threading.Thread(target=slack_rest_watchdog, daemon=True).start()
    print('[rest-watchdog] Slack REST auth.test watchdog started (every 5 min)')
    threading.Thread(target=periodic_restart, daemon=True).start()
    print('[periodic-restart] scheduled in 1800s')
    handler.start()
