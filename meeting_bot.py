#!/usr/bin/env python3
"""Slack → Claude → HubSpot meeting bot.

Listens in two external Slack channels — #demos-booked (real demos on the AE
calendar, 30 min) and #conference-meetings (15-min conference touch meetings).
The channel is the authoritative meeting-type signal. When a BDR posts a booking
announcement, Claude parses it and the bot creates/updates HubSpot records
(contact, company, meeting) and replies in-thread with a confirmation.

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
import os
import random
import re
import threading
import time
import json
import requests
from datetime import datetime, timezone, timedelta

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import sheet_sync
import attribution
from claim_logic import claim_decision, SDR_SLACK, SDR_SLACK_REV, claim_cap, count_company_rows, mark_claimed
import calendar_credit
import recycle


# --- Credentials (all from env; fail fast if missing) ---
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
SLACK_APP_TOKEN = os.environ['SLACK_APP_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
HS_API_KEY = os.environ['HS_API_KEY']
HS = {'Authorization': f'Bearer {HS_API_KEY}', 'Content-Type': 'application/json'}

CREDIT_BY_CALENDAR = os.environ.get('CREDIT_BY_CALENDAR', '0') == '1'
# Second gate: with CREDIT_BY_CALENDAR on but this OFF, the credit path resolves,
# nudges, and audits but performs NO owner writes — the nudge-only observation
# window. Only flip this on once nudges have been verified against real bookings.
CREDIT_ASSIGN_ENABLED = os.environ.get('CREDIT_ASSIGN_ENABLED', '0') == '1'
_AE_EMAIL_MAP = None  # lazily built once per process

# Demos are higher-intent than conference touches, so #demos-booked bookings DO
# auto-create a Scheduled-stage deal (Zain, 2026-08-13). Separate switch so it's
# independent of the conference flag above. Set CREATE_DEMO_DEALS=0 to kill it.
CREATE_DEMO_DEALS = os.environ.get('CREATE_DEMO_DEALS', '1') == '1'

# --- Channel-driven meeting type (the channel is the authoritative signal) ---
# Two external Slack channels feed the bot, each meaning a different thing:
#   demos-booked       -> real demos on the AE's calendar (GCal-synced); 30 min.
#   conference-meetings-> 15-min conference touch meetings; tag as 'conference'.
# The Slack channel decides meeting_type/duration; the post text still decides
# source_channel (email/linkedin/...) and conference_source. This replaces
# guessing the type from header tags like "DEMO" vs "TARGET MARKETS MEETING".
DEMOS_BOOKED_CHANNEL = 'C0AJL106QJJ'
CONFERENCE_MEETINGS_CHANNEL = 'C0B9Z8562RL'
# AE blitz is owned by conference_buddy now — meetingbuddy runs no blitz leaderboard.
# It only keeps EXCLUDING this channel from booking processing so a blitz post can
# never leak into the CRM (defense-in-depth if the bot is ever a member there).
AE_BLITZ_CHANNEL_ID = os.environ.get('BLITZ_CHANNEL_ID')
CHANNEL_PROFILE = {
    DEMOS_BOOKED_CHANNEL:        {'meeting_type': 'demo',       'is_conference': False, 'duration_min': 30},
    CONFERENCE_MEETINGS_CHANNEL: {'meeting_type': 'conference', 'is_conference': True,  'duration_min': 15},
}

# --- Slack user → HubSpot owner mapping ---
SLACK_USER_TO_HS_OWNER = {
    # Fill these in after one-time lookup (users.list). Map by slack user_id.
    # Real-time bot will auto-populate on first message from a user.
}
# Fallback by display-name substring (case-insensitive)
# REAL BDR roster: Zain, Jacob, Dani, Ben Trotter, Matt Stapleton. Everyone
# else (Aman, Bobby, Mike, Nia, Gavin, Kush, Logan, etc.) is an AE/rep/teammate
# — they may attend or react but they do NOT source meetings for the dashboard.
# Match on last names for Ben/Matt: 'ben'/'matt' are unsafe substrings
# (benjamin/bennett, matthew), but 'trotter'/'stapleton' are unambiguous.
NAME_TO_OWNER = {
    'zain': '88760040',
    'jacob': '162210484',
    'dani': '82377567',
    'daniella': '82377567',
    'trotter': '164943105',   # Ben Trotter
    'stapleton': '92184259',  # Matt Stapleton
}

_REACT_EMOJIS = [
    'white_check_mark', 'rocket', 'fire', 'tada', 'moneybag',
    'zap', 'star2', 'muscle', 'raised_hands', 'chart_with_upwards_trend',
]

def _random_react(client_or_requests, channel, ts, count=1):
    """Add random celebration reactions. count>1 for live bookings going crazy."""
    is_sdk = hasattr(client_or_requests, 'reactions_add')
    for name in random.sample(_REACT_EMOJIS, min(count, len(_REACT_EMOJIS))):
        try:
            if is_sdk:
                client_or_requests.reactions_add(channel=channel, timestamp=ts, name=name)
            else:
                requests.post('https://slack.com/api/reactions.add', headers=SLK,
                              data={'channel': channel, 'timestamp': ts, 'name': name}, timeout=10)
        except Exception:
            pass


# --- Claude parser ---
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

PARSE_PROMPT = """You parse Slack meeting announcements from a sales team.

People write these in many different ways — sloppy, abbreviated, missing fields, unusual formatting. Your job is to extract whatever booking information is there regardless of format. When in doubt about any field, make your best guess from context.

Examples of what you might see:
- "Meeting booked! Contact: Jane Doe, COO. Company: Acme Insurance. Demo on 4/30 at 10am. Source: LinkedIn."
- A structured block with a header like "TARGET MARKETS MEETING!" or "DEMO BOOKED!" followed by name/company/date/source/location lines
- "DEMO BOOKED! Chris Bennett (VP Biz Dev) + Chris Jackson (Director) - @BevCap Management / Tuesday Aug 4 10:30am PST / Source: Cold calls / Zoom"
- Emojis, typos, missing punctuation, emoji-only headers, all caps, Slack @mentions mixed in

When multiple attendees appear (e.g. "A + B - Company"), use the FIRST person as the contact; the others are co-attendees, not separate bookings.

MULTIPLE BOOKINGS: if the post clearly announces meetings with DIFFERENT COMPANIES, call log_bookings once per company. Same company = one booking even with multiple attendees.

meeting_type inference:
  - "DEMO" / "demo booked" / demos-booked channel → "demo"
  - "TARGET MARKETS" / "TMPAA" / "TMPCC" / conference header → "conference"
  - Otherwise infer from context or null

conference_source rules:
  - Set ONLY from the event named in the header or Source line.
  - DO NOT infer from company name — "InsurTech NY Holdings" is a company, not a conference.
  - Synonyms → "tmpaa": TMPAA, TMPCC, Target Markets, Target Markets Mid-Year, Target Markets Annual
  - Synonyms → "insurtech_ny_spring": ITNY, InsurTech NY
  - Synonyms → "insurtech_insights": Insurtech Insights, IIUSA, Insurance Innovators USA
  - Synonyms → "insurance_innovators": Insurance Innovators, MFLive
  - Synonyms → "rims_riskworld": RIMS, RIMS RiskWorld
  - Synonyms → "wsia_uw_summit": WSIA, WSIA UW Summit
  - Synonyms → "reuters_es": Reuters E&S, E&S Reuters, Reuters - The Insurer E&S, E&S Insurer
  - Synonyms → "reuters_program_managers": Reuters Program Managers, Program Managers Conference, The Insurer Program Manager
  - Synonyms → "future_of_insurance": Future of Insurance, Reuters Future of Insurance, FOI
  - Synonyms → "insurance_fest": Insurance Fest, InsuranceFest
  - Synonyms → "insurance_insider": Insurance Insider
  - Synonyms → "nashville_dinner": Nashville Dinner
  - Synonyms → "ny_dinner": NY Dinner
  - conference_name_raw: the LITERAL event name as written, taken ONLY from an
    explicit "Event Source:" / "Source:" line or a conference header (e.g. "BTC 2026").
    Do NOT infer it from the company name or stray words. Null if none is named.

source_channel mapping (be liberal — map anything that's close):
  - Email / cold email → "email"
  - LinkedIn / DM / connection → "linkedin"
  - Call / phone / cold call / cold calls / cold calling / dialed → "call"
  - Referral / intro / introduction / referred by → "referral"
  - Inbound / they reached out / they contacted us → "inbound"
  - Conference platform (Brella, conference name, booth, table) → "conference"

is_booking=true for ANY of: a person's name + company + date/time, headers like "BOOKED" / "DEMO BOOKED" / "MEETING BOOKED", "demo with X", "call with X", "meeting with X" + date. Slack formatting (asterisks, underscores, @mentions) doesn't change the meaning.
segment: the prospect COMPANY's role in the insurance value chain.
  - Broker / brokerage / wholesaler / retail broker → "brokerage"
  - Carrier / insurer / reinsurer / risk-bearer → "carrier"
  - MGA / MGU / program administrator / program manager / delegated authority → "mga"
  - If not stated, infer it from what you know about the named company; null only when genuinely unsure.

company_size: the prospect company's employee ("ee") count. Use the number if stated
(e.g. "Size: 500"). If not stated, give your best estimate for a well-known company
(a round number or range like "5000" or "1000-5000"); null only when you have no idea.

is_booking=false ONLY for: pure chat, internal coordination, availability questions, FYIs with no new meeting.

When in doubt: is_booking=true. False negatives (missed bookings) are worse than false positives.

Omit fields that truly aren't mentioned — use null. Strip leading @ from company names.
"""

# Tool schema for structured extraction — forces Claude to return valid JSON (no prose possible).
_BOOKING_ITEM = {
    'type': 'object',
    'properties': {
        'is_booking':          {'type': 'boolean'},
        'contact_first_name':  {'type': ['string', 'null']},
        'contact_last_name':   {'type': ['string', 'null']},
        'contact_title':       {'type': ['string', 'null']},
        'contact_email':       {'type': ['string', 'null']},
        'contact_linkedin':    {'type': ['string', 'null']},
        'company_name':        {'type': ['string', 'null']},
        'meeting_type':        {'type': ['string', 'null'], 'enum': ['intro', 'demo', 'scoping', 'discovery', 'followup', 'checkin', 'conference', None]},
        'source_channel':      {'type': ['string', 'null'], 'enum': ['email', 'linkedin', 'referral', 'call', 'conference', 'inbound', None]},
        'conference_source':   {'type': ['string', 'null'], 'enum': ['wsia_uw_summit', 'wsia_dinner', 'insurtech_ny_spring', 'insurtech_insights', 'insurance_innovators', 'tmpaa', 'tmpcc', 'iiusa', 'rims_riskworld', 'nashville_dinner', 'ny_dinner', 'insurance_insider', 'reuters_es', 'reuters_program_managers', 'future_of_insurance', 'insurance_fest', 'other', None]},
        'conference_name_raw': {'type': ['string', 'null']},
        'segment':             {'type': ['string', 'null'], 'enum': ['brokerage', 'carrier', 'mga', None]},
        'company_size':        {'type': ['string', 'null'], 'description': 'employee/ee count, e.g. "500" or "10k". Best estimate for well-known companies.'},
        'meeting_date':        {'type': ['string', 'null'], 'description': 'YYYY-MM-DD'},
        'meeting_time_utc':    {'type': ['string', 'null'], 'description': 'HH:MM in UTC'},
        'location':            {'type': ['string', 'null']},
        'notes':               {'type': ['string', 'null']},
    },
    'required': ['is_booking'],
}

PARSE_TOOL = {
    'name': 'log_bookings',
    'description': 'Log all meeting bookings found in the Slack post.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'bookings': {'type': 'array', 'items': _BOOKING_ITEM},
        },
        'required': ['bookings'],
    },
}


def parse_with_claude(text, reference_date=None):
    if not client:
        return None
    ref = reference_date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    prompt = (PARSE_PROMPT
              + f"\n[Reference date: {ref}. Dates without a year → pick the year that puts "
                f"the meeting AFTER the reference date. Never default to past years.]\n\n"
              + text)
    try:
        r = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            tools=[PARSE_TOOL],
            tool_choice={'type': 'tool', 'name': 'log_bookings'},
            messages=[{'role': 'user', 'content': prompt}],
        )
        tool_block = next(b for b in r.content if b.type == 'tool_use')
        bookings = tool_block.input.get('bookings', [])
        return bookings if len(bookings) != 1 else bookings[0]
    except Exception as e:
        print(f'Claude parse error: {e}', flush=True)
        return {'_parse_error': str(e)}


# --- HubSpot helpers ---
def hs_find_contact(first, last, email=None, company_name=None):
    """Collision-safe contact resolution (see attribution.find_contact).

    Email wins outright; same-name collisions are disambiguated by the posted
    company; if still ambiguous it returns None rather than guessing — guessing
    is what stamped one BDR's meeting onto another's record."""
    return attribution.find_contact(HS, first, last, email, company_name)


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
    sdr_val = sheet_sync.bdr_sdr_owner_value(owner_id)
    if sdr_val:
        props['sdr_owner'] = sdr_val
    body = {'properties': props}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts', headers=HS, json=body, timeout=30)
    return r.json() if r.status_code in (200, 201) else None


def hs_set_contact_sdr_owner(contact_id, owner_id):
    """Fill a contact's sdr_owner with the sourcing BDR. Never overwrites a
    non-empty value (BDR/AE-curated attribution is canonical). No-op for
    non-BDR owners."""
    val = sheet_sync.bdr_sdr_owner_value(owner_id)
    if not (contact_id and val):
        return
    try:
        r = requests.get(
            f'https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}',
            headers=HS, params={'properties': 'sdr_owner'}, timeout=15)
        current = (r.json().get('properties') or {}).get('sdr_owner') if r.status_code == 200 else None
        if current:
            return  # preserve existing attribution
        requests.patch(
            f'https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}',
            headers=HS, json={'properties': {'sdr_owner': val}}, timeout=15)
    except Exception as e:
        print(f'[sdr_owner] set failed contact={contact_id}: {e}', flush=True)


def hs_associate_contact_company(contact_id, company_id):
    r = requests.put(
        f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/companies/{company_id}',
        headers=HS,
        json=[{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 279}],
        timeout=30)
    return r.status_code in (200, 201)


_CONF_RULES = [
    # More-specific patterns first.
    # BDRs/GCal spell it both "InsurTech" and "InsureTech" — accept either.
    # [\s_] also matches the bot's own bracket-slug titles ("[insurtech_insights]").
    (r'\binsure?tech[\s_]+insights\b',    'insurtech_insights'),
    (r'\binsurance\s+innovators\b',       'insurance_innovators'),
    (r'\binsurance\s+insider\b',          'insurance_insider'),
    (r'\bwsia\s+dinner\b',                'wsia_dinner'),
    (r'\bwsia[\s_]uw[\s_]summit\b',       'wsia_uw_summit'),
    (r'\bwsia\b',                         'wsia_uw_summit'),
    (r'\binsure?tech[\s_]?ny[\s_]?(spring)?\b', 'insurtech_ny_spring'),
    (r'\bitny\d*\b',                      'insurtech_ny_spring'),
    (r'\btmpaa\b',                        'tmpaa'),
    (r'\btmpcc\b',                        'tmpcc'),
    (r'target[\s_]markets',               'tmpaa'),
    (r'\btm[\s_](connect|meeting)\b',     'tmpaa'),
    (r'\brims[\s_]?(riskworld)?\b',       'rims_riskworld'),
    (r'\briskworld\b',                    'rims_riskworld'),
    (r'\bfuture[\s_]+of[\s_]+insurance\b', 'future_of_insurance'),
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


def slugify_conference(name, year):
    """(value, label) for a new conference bucket, or None for junk.
    value = '<name-slug>_<year>', label = '<Name> <year>'. Year always kept."""
    if not name or not year:
        return None
    clean = name.strip()
    core = re.sub(r'[^a-z0-9]+', '_', clean.lower()).strip('_')
    if not core or core.isdigit():
        return None
    core = core[:50 - len(str(year)) - 1].rstrip('_')
    value = f'{core}_{year}'
    label = f'{clean} {year}'
    return value, label


def _year_from(raw, meeting_date):
    m = re.search(r'\b(20\d{2})\b', raw or '')
    if m:
        return int(m.group(1))
    if meeting_date and len(meeting_date) >= 4 and meeting_date[:4].isdigit():
        return int(meeting_date[:4])
    return None


def canonicalize_conference(raw, meeting_date):
    """Best-effort: expand an event acronym to its official name via web search.
    Confident-only — falls back to the raw text on any error/low confidence.
    Returns {'name', 'year'} or None if no year can be derived."""
    year = _year_from(raw, meeting_date)
    if not year:
        return None
    name = (raw or '').strip()
    # Strip a trailing year token from the raw name so the label isn't "BTC 2026 2026".
    name = re.sub(r'\s*\b20\d{2}\b\s*$', '', name).strip() or name
    if not client:
        return {'name': name, 'year': year}
    try:
        sys_prompt = ("You identify insurance/insurtech industry conferences. "
                      "Search the web, then reply with ONLY a JSON object: "
                      '{"name": "<official full name, no year>", "confident": true|false}. '
                      "Set confident=false if you are not sure the acronym maps to a real event.")
        msgs = [{'role': 'user',
                 'content': f'What conference is "{raw}" (an insurance industry event)?'}]
        r = client.messages.create(
            model='claude-opus-4-8', max_tokens=1024,
            tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
            system=sys_prompt, messages=msgs,
        )
        # Server tool loop may pause; re-send up to 3x to let it finish (fixed bound).
        hops = 0
        while r.stop_reason == 'pause_turn' and hops < 3:
            hops += 1
            msgs.append({'role': 'assistant', 'content': r.content})
            r = client.messages.create(
                model='claude-opus-4-8', max_tokens=1024,
                tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
                system=sys_prompt, messages=msgs)
        text = ''.join(b.text for b in r.content if getattr(b, 'type', '') == 'text')
        mjson = re.search(r'\{.*\}', text, re.S)
        if mjson:
            data = json.loads(mjson.group(0))
            if data.get('confident') and data.get('name'):
                return {'name': str(data['name']).strip(), 'year': year}
    except Exception as e:
        print(f'[conf] canonicalize failed for {raw!r}: {e}', flush=True)
    return {'name': name, 'year': year}


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
    ('2026-06-24', '2026-06-26', 'future_of_insurance'),   # ponytail: Chicago; widen if FOI meetings land outside this window
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
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by', 'hs_meeting_outcome', 'hubspot_owner_id', 'hs_meeting_external_url'],
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
                'owner_id': p.get('hubspot_owner_id', ''),
                'external_url': p.get('hs_meeting_external_url') or None,
                'start_iso': p.get('hs_meeting_start_time') or None}
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
                          params={'properties': 'hs_meeting_start_time,meeting_sourced_by,hs_meeting_outcome,hs_meeting_title,hubspot_owner_id,hs_meeting_external_url'},
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
        candidates.append((diff, mid, p.get('meeting_sourced_by', ''), p.get('hubspot_owner_id', ''),
                           p.get('hs_meeting_external_url') or None, start))

    if not candidates:
        return None
    candidates.sort()  # closest match first
    diff, mid, sourced_by, existing_owner, existing_ext_url, existing_start = candidates[0]
    # If we have a target, only accept matches within ±5 days
    if target and diff > 5 * 86400:
        return None
    # Re-fetch title for caller (used for conference auto-tag)
    rg = requests.get(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}', headers=HS,
                      params={'properties': 'hs_meeting_title'}, timeout=10)
    title = (rg.json().get('properties') or {}).get('hs_meeting_title', '') if rg.status_code == 200 else ''
    return {'id': mid, 'sourced_by': sourced_by, 'title': title, 'owner_id': existing_owner,
            'external_url': existing_ext_url, 'start_iso': existing_start}


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


_conf_opts_cache = None
_conf_opts_lock = threading.Lock()
_CONF_PROP_URL = 'https://api.hubapi.com/crm/v3/properties/meetings/conference_source'

def _norm_opt(s):
    """Normalize a value/label for dedup: lowercase, alphanumeric only."""
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

def hs_conference_options(force=False):
    """Cached list of {'value','label','hidden'} for conference_source."""
    global _conf_opts_cache
    if _conf_opts_cache is not None and not force:
        return _conf_opts_cache
    try:
        r = requests.get(_CONF_PROP_URL, headers=HS, timeout=20)
        if r.status_code == 200:
            _conf_opts_cache = [
                {'value': o.get('value'), 'label': o.get('label'), 'hidden': o.get('hidden', False)}
                for o in r.json().get('options', [])]
        else:
            print(f'[conf] options fetch {r.status_code}', flush=True)
            _conf_opts_cache = _conf_opts_cache or []
    except Exception as e:
        print(f'[conf] options fetch error: {e}', flush=True)
        _conf_opts_cache = _conf_opts_cache or []
    return _conf_opts_cache

def hs_add_conference_option(value, label):
    """Append one option; PATCH the full options array (HubSpot replaces the list).
    Refreshes the cache on success."""
    opts = hs_conference_options(force=True)
    if not opts:
        # never PATCH an empty base — a failed/empty read would clobber the live options list
        print('[conf] refusing to add option: options fetch empty/failed', flush=True)
        return False
    payload = [{'label': o['label'], 'value': o['value'], 'hidden': o.get('hidden', False)}
               for o in opts if o.get('value')]
    payload.append({'label': label, 'value': value, 'hidden': False, 'displayOrder': -1})
    try:
        r = requests.patch(_CONF_PROP_URL, headers=HS, json={'options': payload}, timeout=30)
        if r.status_code == 200:
            hs_conference_options(force=True)
            return True
        print(f'[conf] add option {value!r} -> {r.status_code}: {r.text[:200]}', flush=True)
    except Exception as e:
        print(f'[conf] add option error {value!r}: {e}', flush=True)
    return False


def resolve_or_create_conference(raw, meeting_date):
    """Map an unknown conference name to a conference_source value, creating the
    HubSpot option if genuinely new. Returns {'value','created','label'} or None."""
    canon = canonicalize_conference(raw, meeting_date)
    if not canon:
        return None
    slug = slugify_conference(canon['name'], canon['year'])
    if not slug:
        return None
    value, label = slug
    with _conf_opts_lock:
        want = _norm_opt(value)
        want_label = _norm_opt(label)
        for o in hs_conference_options(force=True):   # re-fetch inside lock (race guard)
            if _norm_opt(o.get('value')) == want or _norm_opt(o.get('label')) == want_label:
                return {'value': o['value'], 'created': False, 'label': o.get('label') or o['value']}
        if hs_add_conference_option(value, label):
            return {'value': value, 'created': True, 'label': label}
    return None


def hs_create_meeting(title, date_str, time_str, contact_id, sourced_by, meeting_type, source_channel,
                     conference_source, notes, owner_id=None, company_id=None, duration_min=30):
    # Build start time. NEVER stamp "now" — a fabricated time is the timeless
    # junk we're killing. Callers gate on a real date before reaching here; if
    # one slips through with no date, refuse rather than ghost-create.
    if date_str and time_str:
        try:
            start = datetime.fromisoformat(f'{date_str}T{time_str}:00+00:00')
        except Exception:
            start = datetime.fromisoformat(f'{date_str}T14:00:00+00:00')
    elif date_str:
        start = datetime.fromisoformat(f'{date_str}T14:00:00+00:00')
    else:
        print(f'[meeting] refused create — no date for {title!r}')
        return None
    start_ms = int(start.timestamp() * 1000)
    end_ms = start_ms + duration_min * 60 * 1000
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


# --- Deal creation: conference bookings land in the Sales Pipeline ---
# Per Zain (2026-06-05): bot-booked CONFERENCE meetings should also create a
# deal in the Scheduled stage, so the booking is real in pipeline reporting
# and "how many qualified" becomes deal-stage math instead of guesswork.
# (The AE-side scheduled_deal_sync.py cron does this for AE-owned intro
# meetings but deliberately skips conference meetings and never sees
# BDR-owned ones — this fills that gap at the moment of booking.)
DEAL_PIPELINE = 'default'
DEAL_STAGE_SCHEDULED = '3541233368'
# An open deal in any of these stages already covers the company — never
# create a second one. (Same list scheduled_deal_sync.py uses.)
DEAL_OPEN_STAGES = ['3541233368', '1034884191', 'appointmentscheduled',
                    'qualifiedtobuy', 'decisionmakerboughtin', 'contractsent']
# Real AEs — the only owners a demo deal may land on (mirrors route_meeting_deals.AE_IDS).
# The reconciliation cron finalizes the owner from the AE on the synced calendar invite.
AE_IDS = {'163071452', '96605305', '162894707', '84250910', '165453251',
          '166089614', '165453250', '654909503', '164601691'}
UNASSIGNED = '166833455'   # Unassigned Territory


def demo_deal_owner():
    """Provisional owner for a freshly-booked demo deal: ALWAYS Unassigned
    Territory. At booking time the calendar invite has not synced yet, so we
    cannot know which AE (if any) is on the call. route_meeting_deals is the
    single source of truth — it assigns the AE once, and only if, that AE is on
    the synced invite; otherwise the deal stays in Unassigned for triage.
    (Changed 2026-08-29: previously stamped the account's AE, which dropped
    BDR-booked demos onto AEs who were never on the call — Gavin's report.)"""
    return UNASSIGNED


def hs_find_open_deal(company_id, contact_id):
    """First open Sales-Pipeline deal associated with the company (or, when
    the company is unknown, the contact). None if there is none."""
    for prop, oid in (('associations.company', company_id),
                      ('associations.contact', contact_id)):
        if not oid:
            continue
        body = {'filterGroups': [{'filters': [
                    {'propertyName': prop, 'operator': 'EQ', 'value': str(oid)},
                    {'propertyName': 'pipeline', 'operator': 'EQ', 'value': DEAL_PIPELINE},
                    {'propertyName': 'dealstage', 'operator': 'IN', 'values': DEAL_OPEN_STAGES},
                ]}],
                'properties': ['dealname', 'dealstage', 'hubspot_owner_id'], 'limit': 1}
        r = requests.post('https://api.hubapi.com/crm/v3/objects/deals/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code == 200:
            rs = r.json().get('results', [])
            if rs:
                return rs[0]
    return None


# Recycling guard: a company with a deal in any of these stages is actively
# worked/owned and must never be recycled (Gavin, 2026-09-21) — the open pipeline
# stages plus closed-won. Enforced at claim time as a safety net behind the
# digest's own eligibility filter.
DEAL_COVERED_STAGES = DEAL_OPEN_STAGES + ['closedwon']


def hs_company_has_covered_deal(company_id):
    """True if the company has any deal in an open or closed-won stage. Such an
    account is excluded from recycling. Returns None on API error so the caller
    can distinguish 'no covered deal' (False) from 'couldn't check' (None) and
    avoid handing out a covered account on a transient CRM hiccup."""
    if not company_id:
        return False
    body = {'filterGroups': [{'filters': [
                {'propertyName': 'associations.company', 'operator': 'EQ', 'value': str(company_id)},
                {'propertyName': 'pipeline', 'operator': 'EQ', 'value': DEAL_PIPELINE},
                {'propertyName': 'dealstage', 'operator': 'IN', 'values': DEAL_COVERED_STAGES},
            ]}],
            'properties': ['dealstage'], 'limit': 1}
    try:
        r = requests.post('https://api.hubapi.com/crm/v3/objects/deals/search',
                          headers=HS, json=body, timeout=30)
        if not r.ok:
            return None
        return bool(r.json().get('total', 0))
    except Exception as e:
        print(f'[claim] deal-cover check failed for {company_id}: {e}', flush=True)
        return None


def hs_create_scheduled_deal(company_name, company_id,
                             contact_id, bdr_owner_id, meeting_id):
    """Create a Scheduled-stage deal for a demo booking and associate
    meeting/contact/company. Deal owner = an AE, never a BDR (see
    demo_deal_owner); sourced_by = the booking BDR. Returns deal id or None."""
    props = {
        'dealname': f'{company_name} - Intro Calls',
        'pipeline': DEAL_PIPELINE,
        'dealstage': DEAL_STAGE_SCHEDULED,
        'hubspot_owner_id': demo_deal_owner(),
    }
    if bdr_owner_id:
        props['sourced_by'] = bdr_owner_id
    r = requests.post('https://api.hubapi.com/crm/v3/objects/deals',
                      headers=HS, json={'properties': props}, timeout=30)
    if r.status_code not in (200, 201):
        print(f'[deal] create failed for {company_name}: {r.status_code} {r.text[:200]}')
        return None
    did = r.json().get('id')
    for obj, oid in (('meetings', meeting_id), ('contacts', contact_id),
                     ('companies', company_id)):
        if not oid:
            continue
        try:
            requests.put(
                f'https://api.hubapi.com/crm/v4/objects/deals/{did}/associations/default/{obj}/{oid}',
                headers=HS, timeout=15)
        except Exception:
            pass
    return did


def ensure_deal(channel, conference_source, company_name, company_id,
                contact_id, bdr_owner_id, meeting_id):
    """Booking -> make sure an open Scheduled-stage deal exists. Fires for
    demos (#demos-booked, when CREATE_DEMO_DEALS) only. Conference bookings
    never create a deal (Gavin, 2026-08-21). Always needs a company; never
    duplicates an open deal. Returns '' or a Slack suffix."""
    if channel != DEMOS_BOOKED_CHANNEL:
        return ''  # conference bookings never create a deal (Gavin, 2026-08-21)
    if not CREATE_DEMO_DEALS or not company_name:
        return ''  # demo deal-creation gated off, or no company to attach to
    try:
        if hs_find_open_deal(company_id, contact_id):
            return ''  # a live deal already covers this company
        did = hs_create_scheduled_deal(company_name, company_id,
                                       contact_id, bdr_owner_id, meeting_id)
        if did:
            print(f'[deal] created Scheduled deal {did} for {company_name} (mtg {meeting_id})')
            return ' + deal (Scheduled)'
    except Exception as e:
        print(f'[deal] ensure failed for {company_name}: {e}')
    return ''


# Invert the BDR roster once for owner id -> name; cache HubSpot owner lookups
# for ids not in the roster (AEs, etc.) so we hit /owners/ at most once each.
_OWNER_BY_ID = {oid: name for name, oid in NAME_TO_OWNER.items()}
_OWNER_NAME_CACHE = {}


def _owner_name(owner_id):
    """Display name for a HubSpot owner id. Roster first, then cached API lookup.
    None on failure — callers omit the owner line rather than break."""
    if not owner_id:
        return None
    if owner_id in _OWNER_BY_ID:
        return _OWNER_BY_ID[owner_id]
    if owner_id in _OWNER_NAME_CACHE:
        return _OWNER_NAME_CACHE[owner_id]
    try:
        r = requests.get(f'https://api.hubapi.com/crm/v3/owners/{owner_id}', headers=HS, timeout=15)
        if r.status_code == 200:
            d = r.json()
            name = (d.get('firstName') or '').strip() or (d.get('email') or '').split('@')[0]
            # ponytail: negative-cache — transient owner-lookup failure suppressed until restart
            _OWNER_NAME_CACHE[owner_id] = name or None
            return _OWNER_NAME_CACHE[owner_id]
    except Exception:
        pass
    return None


def _ae_email_map():
    global _AE_EMAIL_MAP
    if _AE_EMAIL_MAP is None:
        try:
            _AE_EMAIL_MAP = calendar_credit.build_ae_email_map(api_key=HS_API_KEY)
        except Exception as e:
            print(f'[credit] owner map build failed: {e}', flush=True)
            _AE_EMAIL_MAP = {}
    return _AE_EMAIL_MAP


def _hs_set_deal_owner(*, deal_id, owner_id):
    """Write hubspot_owner_id on a deal. Used by the booking-path auto-assign.
    Raises on a non-2xx response so a failed write is never audited as success."""
    assert deal_id, 'deal_id required'
    assert owner_id, 'owner_id required'
    r = requests.patch(f'https://api.hubapi.com/crm/v3/objects/deals/{deal_id}',
                       headers=HS, json={'properties': {'hubspot_owner_id': owner_id}}, timeout=15)
    r.raise_for_status()


_OWNER_EMAIL_CACHE = {}


def _owner_email(owner_id):
    """Email for a HubSpot owner id (for calendar impersonation). None on failure."""
    if not owner_id:
        return None
    if owner_id in _OWNER_EMAIL_CACHE:
        return _OWNER_EMAIL_CACHE[owner_id]
    try:
        r = requests.get(f'https://api.hubapi.com/crm/v3/owners/{owner_id}', headers=HS, timeout=15)
        if r.status_code == 200:
            em = (r.json().get('email') or '').strip().lower() or None
            _OWNER_EMAIL_CACHE[owner_id] = em
            return em
    except Exception:
        pass
    return None


def _run_calendar_credit(company_id, contact_id, prospect_email, booker_owner_id,
                         meeting_id, external_url, start_iso, say, ts):
    """Booking-path credit step. No-op unless CREDIT_BY_CALENDAR. Auto-assigns the
    deal to the AE on the invite when unambiguous; posts a nudge on ambiguity."""
    if not CREDIT_BY_CALENDAR:
        return
    try:
        open_deal = hs_find_open_deal(company_id, contact_id)
        if not open_deal:
            return
        deal_id = open_deal['id']
        # incumbent = the deal's current owner if it is an AE
        cur = open_deal.get('properties', {}).get('hubspot_owner_id') or ''
        incumbent = cur if cur in calendar_credit.AE_IDS else None
        booker_email = _owner_email(booker_owner_id)
        if not booker_email:
            print(f'[credit] no email for booker owner {booker_owner_id}; skipping credit for mtg {meeting_id}', flush=True)
            return
        out = calendar_credit.credit_after_booking(
            meeting_id=meeting_id, deal_id=deal_id, deal_owner_id=cur,
            incumbent_ae=incumbent, booker_email=booker_email,
            prospect_email=prospect_email, external_url=external_url, start_iso=start_iso,
            ae_email_map=_ae_email_map(), owner_name_fn=_owner_name,
            assign_enabled=CREDIT_ASSIGN_ENABLED, assign_fn=_hs_set_deal_owner)
        if out.get('action') == 'assign' and out.get('assigned_to'):
            who = _owner_name(out['assigned_to']) or out['assigned_to']
            if out.get('wrote'):
                say(text=f"✓ Credited this deal to *{who}* (on the calendar invite).", thread_ts=ts)
            else:
                say(text=f"👀 Would credit this deal to *{who}* (on the calendar invite) — "
                         f"observation mode, owner left unchanged.", thread_ts=ts)
        elif out.get('text'):
            say(text=out['text'], thread_ts=ts)
    except Exception as e:
        print(f'[credit] booking-path failed for mtg {meeting_id}: {e}', flush=True)


def _hs_search_total(obj, company_id, props):
    """(total, first_result_properties) for `obj` associated to company_id, sorted
    by first prop desc. (0, None) on any failure — a degraded sub-read."""
    try:
        body = {'filterGroups': [{'filters': [
                    {'propertyName': 'associations.company', 'operator': 'EQ', 'value': str(company_id)}]}],
                'properties': props, 'limit': 1}
        if props:
            body['sorts'] = [{'propertyName': props[0], 'direction': 'DESCENDING'}]
        r = requests.post(f'https://api.hubapi.com/crm/v3/objects/{obj}/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code == 200:
            j = r.json()
            results = j.get('results', [])
            return j.get('total', 0), (results[0].get('properties') if results else None)
    except Exception:
        pass
    return 0, None


def _search_objects(obj, company_id, props, sort_prop, limit):
    """List of {'id', **properties} for `obj` associated to company_id, newest
    first. [] on any failure — a degraded sub-read."""
    try:
        body = {'filterGroups': [{'filters': [
                    {'propertyName': 'associations.company', 'operator': 'EQ', 'value': str(company_id)}]}],
                'properties': props,
                'sorts': [{'propertyName': sort_prop, 'direction': 'DESCENDING'}],
                'limit': limit}
        r = requests.post(f'https://api.hubapi.com/crm/v3/objects/{obj}/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code == 200:
            out = []
            for it in r.json().get('results', []):
                row = dict(it.get('properties') or {})
                row['id'] = it.get('id')
                out.append(row)
            return out
    except Exception:
        pass
    return []


def _gather_account_content(company_id):
    """Bounded prior meetings/notes/emails for a company. Each source degrades
    independently to []."""
    return {
        'meetings': _search_objects('meetings', company_id,
            ['hs_meeting_title', 'hs_meeting_start_time', 'hs_meeting_body', 'hs_meeting_outcome'],
            'hs_meeting_start_time', 8),
        'notes': _search_objects('notes', company_id,
            ['hs_note_body', 'hs_timestamp'], 'hs_timestamp', 10),
        'emails': _search_objects('emails', company_id,
            ['hs_email_subject', 'hs_email_text', 'hs_timestamp'], 'hs_timestamp', 10),
    }


def _assoc_contact_ids(obj, obj_ids):
    """Contact ids associated with the given meeting/email ids via a v4 batch
    read. set() on failure — a degraded sub-read."""
    ids = set()
    if not obj_ids:
        return ids
    try:
        r = requests.post(f'https://api.hubapi.com/crm/v4/associations/{obj}/contacts/batch/read',
                          headers=HS, json={'inputs': [{'id': str(i)} for i in obj_ids]}, timeout=30)
        if r.status_code == 200:
            for res in r.json().get('results', []):
                for to in res.get('to', []):
                    tid = to.get('toObjectId')
                    if tid is not None:
                        ids.add(str(tid))
    except Exception:
        pass
    return ids


def _contacts_display(contact_ids):
    """[{'name','title'}] for up to 6 contact ids via a v3 batch read. [] on
    empty/failure."""
    ids = list(contact_ids)[:6]
    if not ids:
        return []
    try:
        r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts/batch/read',
                          headers=HS, json={'properties': ['firstname', 'lastname', 'jobtitle'],
                                            'inputs': [{'id': i} for i in ids]}, timeout=30)
        if r.status_code == 200:
            out = []
            for c in r.json().get('results', []):
                p = c.get('properties') or {}
                name = f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip()
                if name:
                    out.append({'name': name, 'title': p.get('jobtitle') or None})
            return out
    except Exception:
        pass
    return []


def _account_participants(meetings, emails):
    """Distinct meeting/email participant contacts (name + title), capped at 6."""
    ids = _assoc_contact_ids('meetings', [m['id'] for m in meetings if m.get('id')])
    ids |= _assoc_contact_ids('emails', [e['id'] for e in emails if e.get('id')])
    return _contacts_display(ids)


def _clip(s, n):
    """First n chars of s (None-safe)."""
    return (s or '')[:n]


def _summarize_account(meetings, notes, emails):
    """2-3 sentence grounded narrative from HubSpot content, via haiku. None on
    empty input, no client, or any Claude error — never raises."""
    parts = []
    for m in meetings:
        head = _clip(m.get('hs_meeting_title'), 120)
        body = _clip(m.get('hs_meeting_body'), 500)
        if head or body:
            parts.append(f"Meeting: {head}\n{body}".strip())
    for n in notes:
        body = _clip(n.get('hs_note_body'), 500)
        if body:
            parts.append(f"Note: {body}")
    for e in emails:
        subj = _clip(e.get('hs_email_subject'), 120)
        body = _clip(e.get('hs_email_text'), 300)
        if subj or body:
            parts.append(f"Email: {subj}\n{body}".strip())
    blob = '\n\n'.join(parts)[:6000]
    if not blob.strip() or not client:
        return None
    try:
        r = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=180,
            system=("You brief an account executive before a sales call. In 2-3 sentences, "
                    "summarize the prior relationship strictly from the provided HubSpot records "
                    "(meetings, notes, emails). State what was discussed and the current status. "
                    "Do not invent facts; if the records are thin, say so briefly."),
            messages=[{'role': 'user', 'content': blob}])
        text = ''.join(b.text for b in r.content if getattr(b, 'type', None) == 'text').strip()
        return text or None
    except Exception:
        return None


def _last_touch(content, fallback_date):
    """Most-recent (type, date) across gathered content; falls back to the
    company's last-activity date. None if nothing. HubSpot v3 datetimes are ISO
    strings, so lexical max is chronological."""
    cands = [('meeting', m.get('hs_meeting_start_time')) for m in content['meetings']]
    cands += [('note', n.get('hs_timestamp')) for n in content['notes']]
    cands += [('email', e.get('hs_timestamp')) for e in content['emails']]
    cands = [(t, d) for t, d in cands if d]
    if cands:
        t, d = max(cands, key=lambda x: x[1])
        return {'type': t, 'date': _day(d)}
    if fallback_date:
        return {'type': 'activity', 'date': _day(fallback_date)}
    return None


def _day(ts):
    """'2026-06-14T10:00:00Z' -> '2026-06-14'. None-safe."""
    return ts[:10] if ts else None


def hs_company_history(company_id):
    """Prior HubSpot footprint for an EXISTING company, snapshotted before this
    booking's writes. Each sub-read degrades independently. None if nothing found.
    See docs/superpowers/specs/2026-09-02-hubspot-account-history-design.md."""
    if not company_id:
        return None
    h = {}

    # Prior meetings
    m_total, m_first = _hs_search_total('meetings', company_id, ['hs_meeting_start_time'])
    if m_total:
        h['meetings_count'] = m_total
        last = _day((m_first or {}).get('hs_meeting_start_time'))
        if last:
            h['last_meeting_date'] = last

    # Deals: prefer an open one, else most recent
    try:
        body = {'filterGroups': [{'filters': [
                    {'propertyName': 'associations.company', 'operator': 'EQ', 'value': str(company_id)}]}],
                'properties': ['dealname', 'dealstage', 'amount'],
                'sorts': [{'propertyName': 'createdate', 'direction': 'DESCENDING'}], 'limit': 25}
        r = requests.post('https://api.hubapi.com/crm/v3/objects/deals/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code == 200:
            deals = [d.get('properties', {}) for d in r.json().get('results', [])]
            chosen = next((d for d in deals if d.get('dealstage') in DEAL_OPEN_STAGES), deals[0] if deals else None)
            if chosen:
                h['deal'] = {'name': chosen.get('dealname'), 'stage': chosen.get('dealstage'),
                             'amount': chosen.get('amount') or None,
                             'open': chosen.get('dealstage') in DEAL_OPEN_STAGES}
    except Exception:
        pass

    # Known contacts on file
    c_total, _ = _hs_search_total('contacts', company_id, [])
    if c_total:
        h['contacts_count'] = c_total

    # Owner + last activity from the company record
    try:
        r = requests.get(f'https://api.hubapi.com/crm/v3/objects/companies/{company_id}',
                         headers=HS, params={'properties': 'hubspot_owner_id,hs_last_activity_date'}, timeout=15)
        if r.status_code == 200:
            props = r.json().get('properties', {})
            name = _owner_name(props.get('hubspot_owner_id'))
            if name:
                h['owner_name'] = name
            act = _day(props.get('hs_last_activity_date'))
            if act:
                h['last_activity_date'] = act
    except Exception:
        pass

    # Comprehensive brief: prior content -> Claude summary + participants + last touch
    content = _gather_account_content(company_id)
    summary = _summarize_account(content['meetings'], content['notes'], content['emails'])
    if summary:
        h['summary'] = summary
    parts = _account_participants(content['meetings'], content['emails'])
    if parts:
        h['participants'] = parts
    lt = _last_touch(content, h.get('last_activity_date'))
    if lt:
        h['last_touch'] = lt

    return h or None


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


def _is_conference_reply(event, ts):
    """True when a message is a human's thread reply in the conference channel
    (not a top-level booking post, not a thread parent)."""
    tt = event.get('thread_ts')
    return bool(tt) and tt != ts and event.get('channel') == CONFERENCE_MEETINGS_CHANNEL


_CLAIM_LOCKS = {}
_CLAIM_LOCKS_GUARD = threading.Lock()

def _claim_lock(cid):
    with _CLAIM_LOCKS_GUARD:
        return _CLAIM_LOCKS.setdefault(cid, threading.Lock())

_BDR_LOCKS = {}
_BDR_GUARD = threading.Lock()
_WEEK_COUNT = {}          # sdr -> claims counted this rolling week (in-process; race-free vs search lag)

def _bdr_lock(sdr):
    with _BDR_GUARD:
        return _BDR_LOCKS.setdefault(sdr, threading.Lock())

def weekly_claim_count(sdr):
    """Source-of-truth cap: accounts this BDR claimed in the last 7 days, counted from
    HubSpot (cross-message, cross-digest, race-resistant) — not from the Slack message."""
    cutoff_ms = int((time.time() - 7 * 86400) * 1000)
    body = {'filterGroups': [{'filters': [
                {'propertyName': 'last_claim_by', 'operator': 'EQ', 'value': sdr},
                {'propertyName': 'recycle_status', 'operator': 'EQ', 'value': 'active'},
                {'propertyName': 'claim_date', 'operator': 'GTE', 'value': cutoff_ms}]}],
            'limit': 1}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/companies/search',
                      headers=HS, json=body, timeout=30)
    return r.json().get('total', 0) if (r is not None and r.ok) else 0

def create_claim_note(cid, prev, sdr):
    """Log the claim on the company's HubSpot timeline (audit trail). Best-effort."""
    body = {'properties': {
                'hs_note_body': ('♻️ Account recycling: SDR ownership claimed by ' + sdr
                                 + (f' (from {prev})' if prev else '') + ' via the weekly up-for-grabs digest.'),
                'hs_timestamp': int(time.time() * 1000)},
            'associations': [{'to': {'id': cid},
                'types': [{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 190}]}]}
    try:
        requests.post('https://api.hubapi.com/crm/v3/objects/notes', headers=HS, json=body, timeout=30)
    except Exception as e:
        print(f'[claim] note log failed: {e}', flush=True)

@app.action('claim_account')
def handle_claim_account(ack, body, client, action):
    ack()
    cid = str(action['value'])
    uid = body['user']['id']
    ch  = body['channel']['id']
    sdr = SDR_SLACK.get(uid)
    if not sdr:
        client.chat_postEphemeral(channel=ch, user=uid,
                                  text="Only BDRs can claim these — this is BDR account recycling.")
        return
    # even-split cap: each BDR may claim ceil(digest_size / 5) per rolling week. Cap value comes
    # from the digest they're viewing; used-count is tracked in-process under a per-BDR lock
    # (seeded once from HubSpot) so rapid clicks can't race the search index.
    msg  = body.get('message', {})
    ts   = msg.get('ts')
    orig = msg.get('blocks', [])
    cap  = claim_cap(count_company_rows(orig))
    def _update(blocks):
        try:
            client.chat_update(channel=ch, ts=ts, blocks=blocks, text=msg.get('text', 'Up for grabs this week'))
        except Exception as e:
            print(f'[claim] chat_update failed: {e}', flush=True)
    with _bdr_lock(sdr):                                     # serialize a BDR's clicks (cap is race-free)
        if sdr not in _WEEK_COUNT:
            _WEEK_COUNT[sdr] = weekly_claim_count(sdr)       # seed once, then count in memory
        if _WEEK_COUNT[sdr] >= cap:
            client.chat_postEphemeral(channel=ch, user=uid,
                text=f"You've hit your claim limit for this week ({cap}). More open up next Monday.")
            return
        # optimistic: grey the row NOW so it's unclickable instantly (no double-clicks during the swap)
        _update(mark_claimed(orig, cid, sdr))
        with _claim_lock(cid):                               # settle two BDRs racing the same account
            r = requests.get(f'https://api.hubapi.com/crm/v3/objects/companies/{cid}', headers=HS,
                             params={'properties': 'name,sdr_owner,recycle_status,last_claim_by'}, timeout=30)
            if not r or not r.ok:
                _update(orig)                                # restore button — account still available
                client.chat_postEphemeral(channel=ch, user=uid, text="Couldn't reach the CRM — try again."); return
            props = r.json().get('properties', {})
            ok, payload = claim_decision(props, sdr)
            if not ok:
                # already claimed / self — keep greyed, but show the true owner's name
                true_owner = (props.get('last_claim_by') or props.get('sdr_owner') or sdr).strip() or sdr
                _update(mark_claimed(orig, cid, true_owner))
                client.chat_postEphemeral(channel=ch, user=uid, text=payload['reason']); return
            # Exclude accounts with an open or closed-won deal — they're actively
            # worked and not up for grabs (Gavin, 2026-09-21). Safety net behind the
            # digest filter; on a CRM hiccup (None) we don't hand out the account.
            covered = hs_company_has_covered_deal(cid)
            if covered or covered is None:
                _update(orig)                                # restore button — account not claimed
                msg_txt = ("That account has an open or closed-won deal — it's excluded from recycling."
                           if covered else "Couldn't verify deal status — try again.")
                client.chat_postEphemeral(channel=ch, user=uid, text=msg_txt); return
            # claim_decision already sets recycle_status='active' in the payload — that IS
            # the post-claim lifecycle state (clock-eligible like warm), so no extra clear.
            pr = requests.patch(f'https://api.hubapi.com/crm/v3/objects/companies/{cid}', headers=HS,
                                json={'properties': payload}, timeout=30)
            if not pr or not pr.ok:
                _update(orig)                                # restore button — save failed
                client.chat_postEphemeral(channel=ch, user=uid, text="Claim failed to save — try again."); return
            _WEEK_COUNT[sdr] += 1                            # count only a confirmed claim
    name = props.get('name') or cid                         # public greyed row is the confirmation; no ephemeral
    create_claim_note(cid, payload['claimed_from'], sdr)     # log the claim on the HubSpot timeline
    # DM the previous owner, if we can map them
    prev = payload['claimed_from']
    prev_uid = SDR_SLACK_REV.get(prev)
    if prev_uid:
        try:
            client.chat_postMessage(channel=prev_uid,
                text=f"{sdr} claimed *{name}* from you — it was 30+ days cold.")
        except Exception as e:
            print(f'[claim] old-owner DM failed: {e}', flush=True)


# --- Account recycling: READ-ONLY dry-run probe ---
# Computes what the warn/release pipeline WOULD do and only logs it. Sends no
# DMs, writes nothing to HubSpot, posts to no channel. Gated behind RECYCLE_DRY_RUN=1
# so it never runs by accident. Its whole job is to hand real numbers to leadership
# (how many accounts would be pooled, and — critically — whether HubSpot's
# hs_last_activity_date is actually populated) before we arm anything live.

def _fetch_bdr_owned_companies():
    """All companies owned by one of the 5 BDRs, paginated (bounded at 1000).
    Read-only. Returns raw HubSpot result dicts (props under 'properties')."""
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'sdr_owner', 'operator': 'IN',
             'values': sorted(set(SDR_SLACK.values()))},
        ]}],
        'properties': ['name', 'sdr_owner', 'recycle_status', 'hs_last_activity_date', 'createdate'],
        'limit': 100,
    }
    results, after = [], None
    for _ in range(10):                                  # bounded pagination
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/companies/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code != 200:
            print(f'[recycle-dryrun] company search failed: {r.status_code} {r.text[:200]}', flush=True)
            break
        data = r.json()
        results += data.get('results', [])
        after = ((data.get('paging') or {}).get('next') or {}).get('after')
        if not after:
            break
    return results


def recycle_dry_run():
    """Log-only preview of the recycling pipeline. No side effects."""
    now = datetime.now(timezone.utc)
    companies = _fetch_bdr_owned_companies()
    total = len(companies)
    missing_activity = 0
    would_warn = {}                                      # sdr -> count
    would_pool_preview = 0                               # ≥RELEASE_DAYS regardless of warn state
    already = {'warned': 0, 'pool': 0}
    samples = []
    for c in companies:
        p = c.get('properties', {})
        sdr = (p.get('sdr_owner') or '').strip()
        state = (p.get('recycle_status') or '').strip()
        if state in already:
            already[state] += 1
        if recycle.days_since_activity(p, now) is None:
            missing_activity += 1
            continue
        if recycle.decide(p, now, 'warn')['action'] == 'warn':
            would_warn[sdr] = would_warn.get(sdr, 0) + 1
            if len(samples) < 10:
                samples.append(f"{p.get('name') or c.get('id')} ({sdr}, "
                               f"{recycle.days_since_activity(p, now)}d)")
        if recycle.is_cold(p, now, recycle.RELEASE_DAYS):
            would_pool_preview += 1
    print('[recycle-dryrun] ===== READ-ONLY preview (no DMs, no writes) =====', flush=True)
    print(f'[recycle-dryrun] BDR-owned companies: {total}', flush=True)
    print(f'[recycle-dryrun] NO usable date (neither last-activity nor createdate): {missing_activity}'
          f' ({0 if not total else round(100*missing_activity/total)}%) '
          '<- these age-invisible accounts never recycle; should be near 0', flush=True)
    print(f'[recycle-dryrun] would WARN this cycle: {sum(would_warn.values())} '
          f'-> {would_warn}', flush=True)
    print(f'[recycle-dryrun] ≥{recycle.RELEASE_DAYS}d cold (pool volume preview): '
          f'{would_pool_preview}', flush=True)
    print(f'[recycle-dryrun] existing state: {already}', flush=True)
    print(f'[recycle-dryrun] NOTE: covered-deal exclusion not applied here — live '
          'run drops any with an open/closed-won deal, so real numbers are lower.', flush=True)
    for s in samples:
        print(f'[recycle-dryrun]   would-warn e.g. {s}', flush=True)
    print('[recycle-dryrun] ===== end preview =====', flush=True)


def recycle_dry_run_once():
    """Run the dry-run once at startup iff RECYCLE_DRY_RUN=1. Never raises."""
    if os.environ.get('RECYCLE_DRY_RUN') != '1':
        return
    time.sleep(20)                                       # let startup settle
    try:
        recycle_dry_run()
    except Exception as e:
        print(f'[recycle-dryrun] error: {e}', flush=True)


# --- Account recycling: the LIVE warn (Thu) + release (Mon) pipeline ---
# Actions only fire when RECYCLE_ENABLED=1; otherwise each run LOGS what it would
# do (a scheduled dry-run) and touches nothing. Self-serve claim model: release
# posts the pool digest with Claim buttons; ownership only changes when a BDR clicks.

def _hs_set_recycle_status(cid, state):
    """Patch a company's recycle_status (the single lifecycle property, shared with
    the claim flow). Returns True on success."""
    r = requests.patch(f'https://api.hubapi.com/crm/v3/objects/companies/{cid}',
                        headers=HS, json={'properties': {'recycle_status': state}}, timeout=30)
    return bool(r is not None and r.ok)


def _recycle_next_monday(now):
    """(deadline_str, days_left) for the coming Monday release."""
    days_ahead = (0 - now.weekday()) % 7 or 7           # Mon=0; never 'today'
    monday = now + timedelta(days=days_ahead)
    return monday.strftime('%a %b %d').replace(' 0', ' '), days_ahead


def run_recycle(phase, live):
    """Execute one recycle phase ('warn' or 'release') over all BDR-owned companies.
    live=False logs intentions only. Deal-covered candidates are excluded (and
    pulled out of any warned/pool state). Returns a counts dict."""
    now = datetime.now(timezone.utc)
    tag = 'live' if live else 'dry'
    counts = {'warn': 0, 'warn_no_dm': 0, 'release': 0, 'reset': 0, 'excluded_deal': 0}
    pool = []
    for c in _fetch_bdr_owned_companies():
        cid = str(c.get('id'))
        p = c.get('properties', {})
        name = p.get('name') or cid
        sdr = (p.get('sdr_owner') or '').strip()
        d = recycle.decide(p, now, phase)
        act = d['action']
        if act in ('warn', 'release') and hs_company_has_covered_deal(cid) is not False:
            counts['excluded_deal'] += 1
            if (p.get('recycle_status') or '').strip() in (recycle.WARNED, recycle.POOL) and live:
                _hs_set_recycle_status(cid, recycle.WARM)   # covered now — pull it out
            continue
        if act == 'warn':
            deadline, days_left = _recycle_next_monday(now)
            print(f'[recycle][{tag}] WARN {name} ({sdr}) — {d["reason"]}', flush=True)
            if not live:
                counts['warn'] += 1
            else:
                # Only advance to 'warned' if the DM actually reached the owner —
                # otherwise Monday would release the account with NO warning ever
                # sent. An unmapped/failed owner stays warm and is retried next week.
                uid = SDR_SLACK_REV.get(sdr)
                sent = False
                if uid:
                    try:
                        app.client.chat_postMessage(channel=uid,
                            text=recycle.warn_dm_text(name, deadline, days_left))
                        sent = True
                    except Exception as e:
                        print(f'[recycle] DM {sdr} failed: {e}', flush=True)
                if sent:
                    _hs_set_recycle_status(cid, recycle.WARNED)
                    counts['warn'] += 1
                else:
                    counts['warn_no_dm'] += 1
                    print(f'[recycle] no DM for {sdr!r} (unmapped/failed) — left warm, will retry', flush=True)
        elif act == 'release':
            counts['release'] += 1
            pool.append({'id': cid, 'name': name,
                         'note': f'cold {recycle.days_since_activity(p, now)}d'})
            print(f'[recycle][{tag}] RELEASE {name} ({sdr}) — {d["reason"]}', flush=True)
            # NB: recycle_status is set to 'pool' only AFTER the digest posts (below),
            # so a failed post never orphans an account as 'pool' with no Claim button.
        elif act == 'reset':
            counts['reset'] += 1
            if live:
                _hs_set_recycle_status(cid, recycle.WARM)
    if phase == 'release' and pool:
        print(f'[recycle][{tag}] POST digest — {len(pool)} accounts to {recycle.RECYCLE_CHANNEL}', flush=True)
        if live:
            posted = _post_pool_digest(pool)
            if posted < len(pool):
                print(f'[recycle] {len(pool) - posted} account(s) not posted — left warned, '
                      'will retry next cycle', flush=True)
    print(f'[recycle][{tag}] {phase} done: {counts}', flush=True)
    return counts


def _post_pool_digest(pool, chunk=45):
    """Post the up-for-grabs digest in chunks under Slack's 50-block-per-message
    limit (header + one row per account). An account is patched to 'pool' only
    after ITS chunk posts, so a failed/oversized post never leaves it marked
    'pool' with no Claim button. Returns the count actually posted."""
    posted = 0
    for i in range(0, len(pool), chunk):
        part = pool[i:i + chunk]
        try:
            app.client.chat_postMessage(channel=recycle.RECYCLE_CHANNEL,
                blocks=recycle.pool_digest_blocks(part), text='Up for grabs this week')
        except Exception as e:
            print(f'[recycle] digest chunk post failed: {e}', flush=True)
            continue
        for a in part:
            _hs_set_recycle_status(a['id'], recycle.POOL)
        posted += len(part)
    return posted


def recycle_loop():
    """Fire the warn scan on Thursdays and the release+post on Mondays, at most
    once per phase per ISO week. Marker files survive the 30-min os._exit(0)
    restarts (same pattern as sheet_reconcile_loop) so each boot is a no-op until
    the day/week rolls over."""
    time.sleep(90)
    base = os.path.dirname(os.path.abspath(__file__))
    while True:
        try:
            now = datetime.now(timezone.utc)
            phase = {3: 'warn', 0: 'release'}.get(now.weekday())   # Mon=0 .. Thu=3
            if phase:
                marker = os.path.join(base, f'.recycle_{phase}_{now.strftime("%G-W%V")}')
                if not os.path.exists(marker):
                    # Claim the week BEFORE running: periodic_restart() os._exit(0)s
                    # every ~30 min, so writing the marker after a partial run would
                    # let a mid-run kill re-fire the whole phase (double DMs/digest).
                    # The state machine is idempotent (warned/pool -> noop), so a rare
                    # crash just leaves a few accounts for next week's run.
                    with open(marker, 'w') as f:
                        f.write(now.isoformat())
                    live = os.environ.get('RECYCLE_ENABLED') == '1'
                    run_recycle(phase, live)
        except Exception as e:
            print(f'[recycle] loop error: {e}', flush=True)
        time.sleep(3600)


@app.event('message')
def handle_message(event, client, say, logger):
    # Bot messages: skip
    if event.get('bot_id'):
        return
    # AE blitz channel is owned by conference_buddy: ignore its Slack posts
    # entirely (never enter the booking pipeline). Inert unless BLITZ_CHANNEL_ID set.
    if AE_BLITZ_CHANNEL_ID and event.get('channel') == AE_BLITZ_CHANNEL_ID:
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
        thread_ts = msg.get('thread_ts')
    elif subtype:
        # Other subtypes (channel_join, message_deleted, etc.) — skip
        return
    else:
        text = (event.get('text') or '').strip()
        user_id = event.get('user')
        ts = event.get('ts')
        thread_ts = event.get('thread_ts')
    if not text or not ts:
        return
    if _is_conference_reply(event, ts):
        _handle_conference_reply(event['thread_ts'], text, say)
        return
    # Thread replies are discussion (Q&A under an announcement, banter), not new
    # bookings — real bookings are always new top-level posts. Conference-replies are
    # the only actionable threaded case and were handled just above. Skip any other
    # in-thread message so channel chatter is never mis-flagged as a booking.
    if thread_ts and thread_ts != ts:
        print(f'[live] skip: thread reply (not a booking) ts={ts}', flush=True)
        return
    if not _claim_ts(ts):
        print(f'[live] ts={ts} already claimed (sweep beat us) — skipping')
        return

    # Parse — Claude may return a single dict or a list of dicts (multi-booking post)
    parsed_raw = parse_with_claude(text)
    # Parse error (API/JSON failure) — alert in-thread instead of dropping silently.
    # The 30-min periodic-restart replay will re-attempt, but surface it now so a
    # real booking is never lost without anyone noticing.
    if isinstance(parsed_raw, dict) and parsed_raw.get('_parse_error'):
        try:
            say(text="⚠️ I hit an error parsing this and did NOT log it. I'll retry "
                     "automatically on my next restart — or edit/repost to re-trigger.",
                thread_ts=ts)
        except Exception as e:
            print(f'[live] parse-error alert failed: {e}', flush=True)
        return
    if not parsed_raw:
        return
    bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
    bookings = [b for b in bookings if b and b.get('is_booking') and (b.get('contact_first_name') or b.get('contact_last_name') or b.get('contact_email') or b.get('company_name'))]  # require a person or company (drop chatter)
    if not bookings:
        return

    owner_id = slack_user_to_owner(client, user_id)
    # Real bookings are posted by rostered BDRs. A non-BDR poster (exec/teammate
    # chatting in the channel) is almost never a real booking — and the classifier
    # biases toward is_booking=true. So if we can't map the poster to a BDR, skip
    # SILENTLY (log only): no public warning, no celebration emojis, no processing.
    # This prevents embarrassing false positives on ordinary channel discussion.
    # (A genuine booking by an unrostered BDR is recoverable: roster them + repost.)
    if not owner_id:
        print(f'[live] skip: no BDR mapping for slack user={user_id} — treating as '
              f'non-booking chatter, not processed', flush=True)
        return
    channel = event.get('channel')
    print(f'[live] handle_message ts={ts} channel={channel} bookings={len(bookings)}')
    _random_react(client, channel, ts, count=3)
    for parsed in bookings:
        _process_booking(parsed, text, owner_id, ts, client, say, channel=channel, poster=user_id)


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


def _find_meeting_by_booked_at(thread_ts):
    """Find the meeting a conference-thread reply belongs to. The reply's
    thread_ts is the parent booking's ts, and every meeting is stamped with
    booked_at = that ts in ms. Returns {'id','conference_source','meeting_date'}
    or None."""
    try:
        booked_ms = int(float(thread_ts) * 1000)
        r = requests.post(
            'https://api.hubapi.com/crm/v3/objects/meetings/search',
            headers=HS,
            json={'filterGroups': [{'filters': [
                {'propertyName': 'booked_at', 'operator': 'EQ', 'value': str(booked_ms)},
            ]}],
                'properties': ['conference_source', 'hs_meeting_start_time'],
                'limit': 1},
            timeout=15)
        if r.status_code != 200 or not r.json().get('results'):
            return None
        p = r.json()['results'][0]['properties']
        meeting_date = None
        start = p.get('hs_meeting_start_time')
        if start and str(start).isdigit():
            meeting_date = datetime.utcfromtimestamp(int(start) / 1000).strftime('%Y-%m-%d')
        return {'id': r.json()['results'][0]['id'],
                'conference_source': p.get('conference_source'),
                'meeting_date': meeting_date}
    except Exception as e:
        print(f'[conf-reply] lookup failed for thread {thread_ts}: {e}', flush=True)
        return None


def _conf_label(value):
    for o in hs_conference_options():
        if o.get('value') == value:
            return o.get('label') or value
    return value

def _handle_conference_reply(thread_ts, text, say):
    """A human answered the bot's 'which conference?' question in-thread.
    Resolve the reply to a conference_source and re-stamp the meeting.
    Silent no-op if there's no meeting or it already has a real conference."""
    text = (text or '').strip()
    if not text:
        return
    meeting = _find_meeting_by_booked_at(thread_ts)
    if not meeting:
        return
    if meeting['conference_source'] not in (None, 'other'):
        return   # already tagged — ignore ordinary thread chatter
    value = detect_conference_from_title(text)
    label = _conf_label(value) if value else None
    if not value:
        resolved = resolve_or_create_conference(text, meeting.get('meeting_date'))
        if resolved:
            value, label = resolved['value'], resolved['label']
    if not value:
        if say:
            say(text=f'Still couldn\'t identify "{text}" — set it in HubSpot manually.',
                thread_ts=thread_ts)
        return
    try:
        r = requests.patch(
            f"https://api.hubapi.com/crm/v3/objects/meetings/{meeting['id']}",
            headers=HS, json={'properties': {'conference_source': value}}, timeout=30)
        if r.status_code == 200:
            if say:
                say(text=f'✓ tagged {label}', thread_ts=thread_ts)
        else:
            print(f'[conf-reply] patch {meeting["id"]} -> {r.status_code}: {r.text[:200]}', flush=True)
    except Exception as e:
        print(f'[conf-reply] patch error {meeting["id"]}: {e}', flush=True)


def _maybe_unsure_reply(channel, conf, say, ts):
    """In the conference channel, when no conference could be identified, reply
    asking. Live bookings only — sweep/replay pass a silent `say`, which no-ops."""
    if channel == CONFERENCE_MEETINGS_CHANNEL and conf in (None, 'other') and say and ts:
        say(text="Not sure what conference that is — reply with the name and I'll tag it.", thread_ts=ts)


_SEGMENT_LABELS = {'brokerage': 'Brokerage', 'carrier': 'Carrier', 'mga': 'MGA'}


def _log_fields(parsed, is_conference):
    """Aman's meeting-log fields as ordered (label, value) pairs. Segment/Size
    are Claude-inferred; value is None when a field is still missing."""
    seg = _SEGMENT_LABELS.get(parsed.get('segment') or '')
    person = f"{parsed.get('contact_first_name') or ''} {parsed.get('contact_last_name') or ''}".strip()
    fields = [
        ('Person', person or None),
        ('Title', parsed.get('contact_title')),
        ('Company', parsed.get('company_name')),
        ('Segment', seg),
        ('Size', parsed.get('company_size')),
        ('Date/Time', parsed.get('meeting_date')),
        ('Source', parsed.get('source_channel')),
        ('Location', parsed.get('location')),
    ]
    if is_conference:
        conf = parsed.get('conference_source')
        conf = None if conf in (None, 'other') else conf
        fields.insert(0, ('Conference', conf or parsed.get('conference_name_raw')))
    return fields


def _log_comment(parsed, is_conference, poster=None, history=None):
    """Friendly thread comment: share the info the bot researched (Segment/Size)
    and flag any meeting-log fields still missing. Returns '' when there's
    nothing to add. `poster` is the booker's Slack user id (for @mention).

    Slack bots can't edit a human's message, so this is posted as a comment on
    the post rather than edited into it."""
    research = []
    seg = _SEGMENT_LABELS.get(parsed.get('segment') or '')
    if seg:
        research.append(f"• Segment: {seg}")
    if parsed.get('company_size'):
        research.append(f"• Size: ~{parsed['company_size']} employees")
    missing = [label for label, val in _log_fields(parsed, is_conference) if not val]
    if not research and not missing:
        return ''

    who = f"<@{poster}>" if poster else 'team'
    company = parsed.get('company_name') or 'this one'
    lines = []
    if research:
        lines.append(f"Thanks {who}! 🙌 I did some research on *{company}* — here's a bit more for the meeting:")
        lines += research
    else:
        lines.append(f"Thanks {who}! 🙌 Logged your meeting with *{company}*.")
    if missing:
        lines.append("Couldn't find: " + ', '.join(missing) + " — add if you can.")
    if history and history.get('summary'):
        lines.append(f"📋 *{company}* — account history")
        lines.append(history['summary'])
        parts = history.get('participants') or []
        if parts:
            who_talked = ', '.join(p['name'] + (f" ({p['title']})" if p.get('title') else '') for p in parts)
            lines.append(f"• Talked to: {who_talked}")
        deal = history.get('deal')
        if deal and deal.get('name'):
            amt = f", ${deal['amount']}" if deal.get('amount') else ""
            label = 'Open deal' if deal.get('open') else 'Deal'
            lines.append(f"• {label}: {deal['name']} ({deal.get('stage') or ''}{amt})")
        lt = history.get('last_touch')
        if lt and lt.get('date'):
            lines.append(f"• Last touch: {lt['type']}, {lt['date']}")
    elif history:
        hist = []
        if history.get('meetings_count'):
            last = history.get('last_meeting_date')
            hist.append(f"• {history['meetings_count']} prior meetings"
                        + (f" (last: {last})" if last else ""))
        deal = history.get('deal')
        if deal and deal.get('name'):
            amt = f", ${deal['amount']}" if deal.get('amount') else ""
            label = 'Open deal' if deal.get('open') else 'Deal'
            hist.append(f"• {label}: {deal['name']} ({deal.get('stage') or ''}{amt})")
        if history.get('contacts_count'):
            hist.append(f"• {history['contacts_count']} contacts on file")
        if history.get('owner_name'):
            act = history.get('last_activity_date')
            hist.append(f"• Owner: {history['owner_name']}"
                        + (f" · last activity {act}" if act else ""))
        if hist:
            lines.append(f"📋 We've spoken to *{company}* before:")
            lines += hist
    lines.append(f"Nice meeting, {who}! 🎉")
    return '\n'.join(lines)


def _hs_owner_email(owner_id):
    """Look up a HubSpot owner's email (used as the calendar to search). None on miss."""
    if not owner_id:
        return None
    try:
        r = requests.get(f'https://api.hubapi.com/crm/v3/owners/{owner_id}',
                         headers=HS, timeout=15)
        if r.ok:
            return r.json().get('email')
    except Exception as e:
        print(f'[vp-escalate] owner email lookup failed: {e}', flush=True)
    return None


def _vp_thread_has_nudge(client, channel, thread_ts):
    """Dedup using Slack itself: True if a VP+ nudge already exists in this thread.
    Durable across restarts and across every processing path, with no extra
    datastore. Fails open (better a rare dup than to never nudge)."""
    try:
        rep = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        return any('VP+ ICP' in (m.get('text') or '') for m in rep.get('messages', []))
    except Exception as e:
        print(f'[vp-escalate] dedup check failed: {e}', flush=True)
        return False


def _maybe_vp_escalate(parsed, meeting_id, date_str, say, ts, poster=None, owner_id=None,
                       channel=None):
    """VP+ ICP escalation: post exactly ONE nudge in the booking thread asking the
    booker to add Zac or Aman. Posts via the real Slack client so it fires from any
    path (live/replay/sweep, not the sometimes-silent `say`), and dedups by scanning
    the thread so it can never double-post. Fully guarded — never breaks booking.
    (Calendar auto-add is parked pending a reconciler-based rebuild; the nudge is the
    reliable, shipped behavior.)
    """
    try:
        import vp_escalation as vp
        if not vp.enabled() or not (channel and ts):
            return
        contact = vp.contact_from_parsed(parsed)
        meeting = {'id': meeting_id, 'meeting_type': parsed.get('meeting_type', 'demo'),
                   'company': parsed.get('company_name'), 'attendees': []}
        if not vp.is_escalation_candidate(meeting, contact):
            return
        if vp.dry_run():
            print(f'[vp-escalate][dry-run] would nudge mtg={meeting_id}', flush=True)
            return
        if _vp_thread_has_nudge(app.client, channel, ts):
            return  # already nudged this booking — never repeat
        text = vp.nudge_message(poster, meeting, contact)
        app.client.chat_postMessage(channel=channel, thread_ts=ts, text=text)
        print(f'[vp-escalate] nudged mtg={meeting_id}', flush=True)
    except Exception as e:
        print(f'[vp-escalate] skipped (non-fatal): {e}', flush=True)


def _process_booking(parsed, text, owner_id, ts, client, say, channel=None, poster=None):
    # AE/ITC blitz channel: never write anything to HubSpot from here. Blitz is
    # owned by conference_buddy; this guard is defense-in-depth so no caller (live
    # handler, replay, live-sweep) can leak a blitz-channel post into the CRM.
    if channel and AE_BLITZ_CHANNEL_ID and channel == AE_BLITZ_CHANNEL_ID:
        return
    # Test channel: run the real parse -> ICP -> VP+ nudge, but write NOTHING to
    # HubSpot (no contact/company/meeting/deal). Guard lives here so EVERY caller
    # (live handler, replay, live-sweep) is covered. Posts treated as demos.
    _test_ch = os.environ.get('VP_ESCALATION_TEST_CHANNEL')
    if _test_ch and channel == _test_ch:
        parsed['meeting_type'] = 'demo'
        _maybe_vp_escalate(parsed, f'test-{ts}', parsed.get('meeting_date'),
                           say, ts, poster=poster)
        print(f'[test] escalation-only (no HubSpot) in test channel ts={ts}', flush=True)
        return

    company_name = parsed.get('company_name')
    first = parsed.get('contact_first_name')
    last = parsed.get('contact_last_name')
    email = parsed.get('contact_email')

    # Channel is the authoritative meeting-type signal — it overrides whatever
    # Claude guessed from the text. demos-booked -> 'demo'; conference-meetings
    # -> 'conference'. Unknown/legacy channel -> fall back to text inference.
    profile = CHANNEL_PROFILE.get(channel, {})
    if profile.get('meeting_type'):
        parsed['meeting_type'] = profile['meeting_type']
    duration_min = profile.get('duration_min', 30)

    # If a conference is involved, default source_channel to "conference" when
    # Claude couldn't classify it (e.g. "Source: Brella" doesn't match the basic enum).
    if not parsed.get('source_channel') and parsed.get('conference_source'):
        parsed['source_channel'] = 'conference'
    # And default meeting_type to "conference" for conference-sourced meetings
    # unless Claude already classified as something more specific (demo/intro/etc.).
    if not parsed.get('meeting_type') and parsed.get('conference_source'):
        parsed['meeting_type'] = 'conference'

    # New/unknown conference → resolve to a real HubSpot bucket (create if needed).
    # Only when the parser couldn't map it (None or catch-all 'other') and a raw
    # event name is present. Overrides 'other' only when a concrete bucket results.
    if parsed.get('conference_source') in (None, 'other') and parsed.get('conference_name_raw'):
        resolved = resolve_or_create_conference(
            parsed['conference_name_raw'], parsed.get('meeting_date'))
        if resolved:
            parsed['conference_source'] = resolved['value']
            if resolved['created'] and say and ts:
                say(text=f"🆕 New event source *{resolved['label']}* created in HubSpot "
                         f"— reply to rename or merge if that's wrong.", thread_ts=ts)

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
    # Snapshot prior HubSpot footprint BEFORE any writes, so counts exclude the
    # meeting/deal we're about to create AND the contact we may create below.
    # Net-new company (co is None) -> no reads.
    history = hs_company_history(company_id) if co else None

    # 2. Find or create contact (company disambiguates same-name collisions)
    contact = hs_find_contact(first, last, email, company_name)
    if not contact and (first or last or email):
        contact = hs_create_contact(first, last, parsed.get('contact_title'),
                                     company_name, email, parsed.get('contact_linkedin'), owner_id)
    contact_id = contact['id'] if contact else None
    if contact_id:
        hs_set_contact_sdr_owner(contact_id, owner_id)

    if contact_id and company_id:
        hs_associate_contact_company(contact_id, company_id)

    # 3. Date-aware dedup — find the existing meeting closest to the announced date and tag it
    date_str = parsed.get('meeting_date')
    existing = hs_find_existing_meeting(contact_id, date_str) if contact_id else None
    # Hard guard: a contact-pivot match must be for the SAME company named in the
    # post. Protects against any residual same-name contact mismatch tagging a
    # different company's (or another BDR's) meeting. Skip when matched by email
    # (contact is then unambiguous) or when the post named no company.
    if existing and company_name and not email and not attribution.title_matches_company(existing.get('title', ''), company_name):
        existing = None
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
        # Conference booking -> make sure a Scheduled-stage deal exists
        deal_suffix = ensure_deal(channel, conf, company_name, company_id,
                                  contact_id, owner_id, existing['id'])
        _run_calendar_credit(company_id, contact_id, email, owner_id,
                             existing['id'], existing.get('external_url'),
                             existing.get('start_iso'), say, ts)
        portal_id = '44712408'
        mtg_url = f"https://app-na2.hubspot.com/contacts/{portal_id}/record/0-47/{existing['id']}"
        prev = existing['sourced_by']
        action = 'Re-tagged existing meeting' if prev and prev != owner_id else 'Tagged existing meeting'
        say(text=f"✓ {action} (was {prev or 'untagged'}){sheet_result}{deal_suffix}\n{mtg_url}", thread_ts=ts)
        _note = _log_comment(parsed, bool(profile.get('is_conference')), poster, history=history)
        if _note:
            say(text=_note, thread_ts=ts)
        _maybe_unsure_reply(channel, conf, say, ts)
        _maybe_vp_escalate(parsed, existing['id'], date_str, say, ts, poster=poster, owner_id=owner_id, channel=channel)
        return

    # 4. Create meeting — but only with a real time. We reach here only when no
    # existing (incl. GCal-synced) meeting matched. Fabricating a time is the
    # timeless-junk we're killing, so gate on a real date first.
    if not parsed.get('meeting_date'):
        say(text="✓ Logged the contact/company. No meeting created yet — reply with the "
                 "date (and time) and I'll add it.", thread_ts=ts)
        return
    # demos-booked is a real calendar event: require a confirmed time, or wait
    # for the AE's calendar to sync (a later sweep will tag it). Don't ghost it.
    if channel == DEMOS_BOOKED_CHANNEL and not parsed.get('meeting_time_utc'):
        say(text="✓ Logged the contact/company. This is a demo (calendar event) — reply with the "
                 "time, or once it's on the AE's calendar I'll tag it automatically.", thread_ts=ts)
        return

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
        duration_min=duration_min,
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

    # 5. Conference booking -> make sure a Scheduled-stage deal exists
    deal_suffix = ''
    if mtg and mtg.get('id'):
        deal_suffix = ensure_deal(channel, parsed.get('conference_source'),
                                  company_name, company_id,
                                  contact_id, owner_id, mtg['id'])
        _run_calendar_credit(company_id, contact_id, email, owner_id,
                             mtg['id'], None,
                             parsed.get('meeting_time_utc') and f"{parsed.get('meeting_date')}T{parsed.get('meeting_time_utc')}:00Z",
                             say, ts)

    # 6. Push to Ellen's sheet (best-effort)
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

    # 7. Reply
    if mtg and mtg.get('id'):
        portal_id = '44712408'
        mtg_url = f"https://app-na2.hubspot.com/contacts/{portal_id}/record/0-47/{mtg['id']}"
        pieces = []
        if parsed.get('meeting_type'): pieces.append(parsed['meeting_type'])
        if parsed.get('source_channel'): pieces.append(f"via {parsed['source_channel']}")
        if parsed.get('conference_source'): pieces.append(f"@ {parsed['conference_source']}")
        tag = ' · '.join(pieces) if pieces else 'meeting'
        confirmation = (
            f"✓ Logged {tag}{sheet_result}{deal_suffix}\n"
            f"Contact: {first or ''} {last or ''} ({parsed.get('contact_title') or '—'}) @ {company_name or '—'}\n"
            f"{mtg_url}"
        )
        say(text=confirmation, thread_ts=ts)
        note = _log_comment(parsed, bool(profile.get('is_conference')), poster, history=history)
        if note:
            say(text=note, thread_ts=ts)
        _maybe_unsure_reply(channel, parsed.get('conference_source'), say, ts)
        _maybe_vp_escalate(parsed, mtg['id'], parsed.get('meeting_date'), say, ts, poster=poster, owner_id=owner_id, channel=channel)
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


def _vp_retry_pass(client):
    """Re-attempt queued VP+ auto-adds. The GCal invite usually syncs minutes
    after the Slack post, so we keep trying until it appears (or TTL). Idempotent:
    skips if either exec is already on the invite; add_guest itself no-ops on dupes."""
    import vp_escalation as vp
    for ctx in vp.escalation_pending()[:50]:      # bounded
        try:
            eid, organizer = vp.find_calendar_event(ctx['search_as'], ctx['start_iso'], ctx['terms'])
            if not eid:
                continue                          # not synced yet — retry next tick
            if vp.event_has_exec(organizer, eid):
                vp.escalation_remove(ctx['meeting_id'])   # someone already added one
                continue
            r = vp.add_guest(eid, ctx['exec'], calendar_id=organizer)
            if r.get('performed'):
                try:
                    client.chat_postMessage(
                        channel=ctx['channel'], thread_ts=ctx['thread_ts'],
                        text=f":white_check_mark: Auto-added *{ctx['exec'].capitalize()}* "
                             f"to the invite.")
                except Exception as e:
                    print(f'[vp-retry] post failed: {e}', flush=True)
                vp.escalation_remove(ctx['meeting_id'])
                print(f"[vp-retry] auto-added {ctx['exec']} to event {eid} "
                      f"mtg={ctx['meeting_id']}", flush=True)
            elif r.get('reason') == 'already_present':
                vp.escalation_remove(ctx['meeting_id'])
        except Exception as e:
            print(f"[vp-retry] error mtg={ctx.get('meeting_id')}: {e}", flush=True)


def vp_retry_loop():
    """Background loop driving _vp_retry_pass every 2 min (Bug-1 fix)."""
    import vp_escalation as vp
    while True:
        try:
            if vp.enabled() and not vp.dry_run():
                _vp_retry_pass(app.client)
        except Exception as e:
            print(f'[vp-retry] loop error: {e}', flush=True)
        time.sleep(120)


# ── Exec-attach sweep: add Zac/Aman to VP+ ICP demos once the invite syncs ────
def _ea_to_int(v):
    try:
        return int(float(str(v)))
    except Exception:
        return None


def _meeting_icp_fields(meeting_id):
    """(jobtitle, employees, segment) for a meeting's primary contact, from HubSpot."""
    try:
        a = requests.get(f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/contacts',
                         headers=HS, timeout=15)
        res = a.json().get('results', []) if a.ok else []
        if not res:
            return (None, None, None)
        cid = res[0].get('toObjectId') or res[0].get('id')
        c = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                         headers=HS, params={'properties': 'jobtitle'}, timeout=15)
        jt = (c.json().get('properties', {}) or {}).get('jobtitle') if c.ok else None
        emp, seg = None, None
        ca = requests.get(f'https://api.hubapi.com/crm/v4/objects/contacts/{cid}/associations/companies',
                          headers=HS, timeout=15)
        cres = ca.json().get('results', []) if ca.ok else []
        if cres:
            coid = cres[0].get('toObjectId') or cres[0].get('id')
            co = requests.get(f'https://api.hubapi.com/crm/v3/objects/companies/{coid}',
                              headers=HS, params={'properties': 'numberofemployees,segment,type'}, timeout=15)
            cp = co.json().get('properties', {}) if co.ok else {}
            emp = _ea_to_int(cp.get('numberofemployees'))
            seg = cp.get('segment') or cp.get('type')
        return (jt, emp, seg)
    except Exception as e:
        print(f'[exec-attach] icp-fields error mtg={meeting_id}: {e}', flush=True)
        return (None, None, None)


_ATTACH_DONE = set()   # meeting ids handled this process (avoid re-querying every tick)


def exec_attach_pass(client):
    """One sweep: for recently-booked demos whose Google invite has synced, add the
    freer exec to the real event (deterministic id from external_url; no search)."""
    import vp_escalation as vp
    from datetime import datetime, timezone, timedelta
    lo = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp() * 1000)
    # meeting_type == 'demo' is REQUIRED: without it the sweep matches every synced
    # meeting (internal syncs, customer implementation sessions, check-ins, conference
    # touches) whose contact happens to be VP+ — and would add execs to all of them.
    body = {"filterGroups": [{"filters": [
                {"propertyName": "hs_meeting_external_url", "operator": "HAS_PROPERTY"},
                {"propertyName": "hs_createdate", "operator": "GTE", "value": str(lo)},
                {"propertyName": "meeting_type", "operator": "EQ", "value": "demo"}]}],
            "properties": ["hs_meeting_external_url", "hs_meeting_start_time", "hs_meeting_title"],
            "sorts": [{"propertyName": "hs_createdate", "direction": "DESCENDING"}], "limit": 50}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                      headers=HS, json=body, timeout=30)
    if not r.ok:
        print(f'[exec-attach] search failed {r.status_code}', flush=True)
        return
    for m in r.json().get('results', [])[:50]:      # bounded
        mid = m.get('id')
        if not mid or mid in _ATTACH_DONE:
            continue
        p = m.get('properties', {})
        event_id, organizer = vp.decode_event_url(p.get('hs_meeting_external_url'))
        if not (event_id and organizer):
            continue
        jt, emp, seg = _meeting_icp_fields(mid)
        if not vp.exec_attach_ok(jt, emp, seg):
            _ATTACH_DONE.add(mid)                    # not a target — don't recheck
            continue
        if vp.event_has_exec(organizer, event_id):
            _ATTACH_DONE.add(mid)                    # already covered
            continue
        start_iso = p.get('hs_meeting_start_time')
        busy = ({w: (vp.freebusy(w, start_iso, start_iso) or 0) for w in ('aman', 'zac')}
                if start_iso else {'aman': 0, 'zac': 0})
        who = vp.pick_exec(busy)
        # Sweep has its own dry-run flag so we can observe it while the nudge stays live.
        if os.environ.get('VP_EXEC_ATTACH_DRYRUN', '1') != '0':
            print(f"[exec-attach][dry-run] would add {who} to event {event_id} "
                  f"org={organizer} mtg={mid} title={p.get('hs_meeting_title')}", flush=True)
            _ATTACH_DONE.add(mid)                    # log once per process
            continue
        res = vp.add_guest(event_id, who, calendar_id=organizer)
        if res.get('performed') or res.get('reason') == 'already_present':
            _ATTACH_DONE.add(mid)
            print(f"[exec-attach] added {who} to event {event_id} mtg={mid}", flush=True)


def exec_attach_loop():
    """Every 2 min: attach execs to VP+ ICP demos whose invite has synced."""
    import vp_escalation as vp
    while True:
        try:
            if vp.enabled():
                exec_attach_pass(app.client)
        except Exception as e:
            print(f'[exec-attach] loop error: {e}', flush=True)
        time.sleep(120)


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
            # Blitz channel is owned by conference_buddy: never process its posts as bookings
            if AE_BLITZ_CHANNEL_ID and cid == AE_BLITZ_CHANNEL_ID:
                continue
            try:
                parsed_raw = parse_with_claude(text)
            except Exception:
                continue
            if not parsed_raw:
                continue
            bookings = parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]
            bookings = [b for b in bookings if b and b.get('is_booking') and (b.get('contact_first_name') or b.get('contact_last_name') or b.get('contact_email') or b.get('company_name'))]  # require a person or company (drop chatter)
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
                    continue
            except Exception as e:
                print(f'[replay] dedup check failed ts={ts}: {e} — falling through')
            any_ok = False
            for parsed in bookings:
                try:
                    _process_booking(parsed, text, owner_id, ts, app.client, silent_say, channel=cid)
                    processed += 1
                    any_ok = True
                except Exception as e:
                    print(f'[replay] process error ts={ts}: {e}')
        print(f'[replay] {ch.get("name")}: {kept} booking-shaped, {processed} processed (cumulative)')
    print(f'[replay] done — re-processed {processed} booking(s) from last 24h')


# --- Reconciler: merge bot-created/GCal twin pairs ---
# Race: bot fires on Slack post → no GCal meeting yet → bot creates one. Later
# GCal syncs the real calendar event, leaving a duplicate untagged copy.
# This sweep finds those pairs (same owner + same day, one has booked_at, other
# doesn't, one's title starts with "FurtherAI + ") and merges metadata onto
# the GCal copy, then deletes the bot-created duplicate.
def _fetch_reconcile_candidates():
    """All meetings starting now-3d .. now+45d, paginated (bounded at 1000).

    Keyed on START time, not hs_createdate: the bot record is created the
    moment the BDR posts, but the GCal copy syncs whenever the calendar invite
    lands — often days or weeks later. The old 6h-createdate window could never
    see such a pair together, which is how twin records survived to inflate
    conference counts ~1.7x (151 raw vs 83 real, InsurTech NY Jun 2026)."""
    lo = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)
    hi = int((datetime.now(timezone.utc) + timedelta(days=45)).timestamp() * 1000)
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_meeting_start_time', 'operator': 'BETWEEN',
             'value': str(lo), 'highValue': str(hi)},
        ]}],
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by',
                       'booked_at', 'hubspot_owner_id', 'meeting_type',
                       'meeting_source_channel', 'conference_source', 'hs_timestamp'],
        'limit': 100,
    }
    results = []
    after = None
    for _ in range(10):  # bounded pagination
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        results += data.get('results', [])
        after = ((data.get('paging') or {}).get('next') or {}).get('after')
        if not after:
            break
    return results


def _meeting_contacts(meeting_id):
    """(contact ids, normalized person names) associated with a meeting."""
    ids, names = set(), set()
    try:
        r = requests.get(
            f'https://api.hubapi.com/crm/v4/objects/meetings/{meeting_id}/associations/contacts',
            headers=HS, timeout=15)
        if r.status_code != 200:
            return ids, names
        for a in r.json().get('results', [])[:10]:
            cid = str(a['toObjectId'])
            ids.add(cid)
            rc = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                              headers=HS, params={'properties': 'firstname,lastname'},
                              timeout=10)
            if rc.status_code == 200:
                p = rc.json().get('properties') or {}
                nm = re.sub(r'\s+', ' ',
                            f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip().lower())
                if nm:
                    names.add(nm)
    except Exception:
        pass
    return ids, names


def _same_meeting_contacts(id_a, id_b):
    """False when two meetings demonstrably involve DIFFERENT people: both
    have contacts, no shared contact id, and no shared person name. Guards the
    twin-merge against same-company-different-person pairs (real case: two
    Markel walk-ups 55min apart at InsurTech NY — company+time matched, but
    merging would have deleted Karen's meeting into Allison's). The NAME
    fallback matters because the bot and GCal often hold separate contact
    records for the same human (one created without an email)."""
    ids_a, names_a = _meeting_contacts(id_a)
    ids_b, names_b = _meeting_contacts(id_b)
    if not ids_a or not ids_b:
        return True  # can't verify -> company+time guards are the backstop
    return bool((ids_a & ids_b) or (names_a & names_b))


def reconcile_duplicates():
    results = _fetch_reconcile_candidates()
    if not results:
        return 0
    by_key = {}
    for m in results:
        p = m.get('properties') or {}
        st = p.get('hs_meeting_start_time')
        if not st:
            continue
        try:
            start_dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
        except Exception:
            continue
        # Group by start DATE only. The old (owner, date) key missed real
        # twins: the GCal copy is owned by whoever's calendar synced (often
        # the AE) while the bot copy is owned by the BDR. Company-token
        # containment + the ±2h window below are the actual safety guards.
        by_key.setdefault(start_dt.date().isoformat(), []).append((m, start_dt, p))

    def _norm_company(title):
        if not title: return ''
        t = re.sub(r'^FurtherAI\s*\+\s*', '', title)
        t = re.sub(r'\s*\[[^\]]*\]\s*', '', t)
        t = re.sub(r'\s*\([^)]*\)\s*', '', t)
        return re.sub(r'[^a-z0-9]', '', t.lower())

    merged = 0
    for items in by_key.values():
        if len(items) < 2:
            continue
        # Find bot-created (has booked_at + title prefix) and GCal twin (no booked_at, ±2h).
        # Safety: require the bot's company name to appear in the GCal title — without this,
        # an unrelated GCal meeting at a nearby time on the same day would absorb the bot's
        # booked_at/sourced_by tags and the bot record would be deleted (we saw this destroy
        # a Daiichi booking by merging it into an unrelated Berkshire meeting).
        consumed = set()
        for bot_m, bot_dt, bot_p in items:
            if bot_m['id'] in consumed:
                continue
            if not bot_p.get('booked_at'):
                continue
            if not (bot_p.get('hs_meeting_title') or '').startswith('FurtherAI + '):
                continue
            bot_company_norm = _norm_company(bot_p.get('hs_meeting_title'))
            for gcal_m, gcal_dt, gcal_p in items:
                if gcal_m['id'] == bot_m['id']:
                    continue
                if gcal_m['id'] in consumed:
                    continue
                if gcal_p.get('booked_at'):
                    continue
                if abs((bot_dt - gcal_dt).total_seconds()) > 7200:
                    continue
                gcal_title_norm = re.sub(r'[^a-z0-9]', '',
                                          (gcal_p.get('hs_meeting_title') or '').lower())
                if not bot_company_norm or not gcal_title_norm:
                    continue
                if bot_company_norm not in gcal_title_norm and gcal_title_norm not in bot_company_norm:
                    continue
                # Contact guard: same company + close time is NOT enough —
                # two different people at one company are two real meetings.
                if not _same_meeting_contacts(bot_m['id'], gcal_m['id']):
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
                consumed.add(bot_m['id'])
                consumed.add(gcal_m['id'])
                # Refresh in-memory props so a later iteration sees the new state
                for k, v in merge_props.items():
                    gcal_p[k] = v
                break

    # Second pass: same owner + same exact start_time, fuzzy-similar company name.
    # Catches the case where the bot couldn't dedup against an existing GCal
    # meeting (e.g. spelling diff: "Franklin Maddison" vs "Franklin Madison")
    # so it created a parallel record. Both end up with booked_at set, which the
    # first pass skips. Here we pair by exact start-time + name similarity.
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
    for m in results:
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
                # Contact guard (same rationale as pass 1)
                if not _same_meeting_contacts(m_a['id'], m_b['id']): continue
                # Winner selection:
                # 1. If exactly one side has booked_at, that side WINS (never delete a BDR-tagged meeting).
                # 2. Otherwise prefer the one WITHOUT a [bracket] tag in title (= GCal copy).
                booked_a = bool(p_a.get('booked_at'))
                booked_b = bool(p_b.get('booked_at'))
                if booked_a and not booked_b:
                    winner, loser, wp, lp = m_a, m_b, p_a, p_b
                elif booked_b and not booked_a:
                    winner, loser, wp, lp = m_b, m_a, p_b, p_a
                else:
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


def conference_tag_sweep():
    """Stamp conference_source on meetings that are missing it.

    GCal-synced meetings never pass through _process_booking, so they carry no
    conference_source even when they ARE the conference meeting (after the
    reconciler merges a twin pair, the surviving GCal copy inherits the tag —
    but un-twinned GCal meetings stay blank). This sweep makes "all meetings
    for event X" a single property filter instead of title-regex archaeology:
      - title names the event -> stamp it (any meeting), OR
      - start falls inside a conference date window AND the meeting is
        BDR-sourced -> stamp from the window.
    Internal/customer syncs survive both tests (no event in the title, no
    meeting_sourced_by), so they are never mis-tagged."""
    now = datetime.now(timezone.utc)
    lo = int((now - timedelta(days=7)).timestamp() * 1000)
    hi = int((now + timedelta(days=60)).timestamp() * 1000)
    body = {
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_meeting_start_time', 'operator': 'BETWEEN',
             'value': str(lo), 'highValue': str(hi)},
            {'propertyName': 'conference_source', 'operator': 'NOT_HAS_PROPERTY'},
        ]}],
        'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by'],
        'limit': 100,
    }
    tagged = 0
    after = None
    for _ in range(10):  # bounded pagination
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        for m in data.get('results', []):
            p = m.get('properties') or {}
            conf = detect_conference_from_title(p.get('hs_meeting_title'))
            if not conf and p.get('meeting_sourced_by'):
                conf = detect_conference_from_date((p.get('hs_meeting_start_time') or '')[:10])
            if not conf:
                continue
            rp = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{m["id"]}',
                                headers=HS,
                                json={'properties': {'conference_source': conf}},
                                timeout=30)
            if rp.status_code == 200:
                tagged += 1
                print(f'[conf-tag] {m["id"]} <- {conf} ({(p.get("hs_meeting_title") or "")[:50]!r})')
        after = ((data.get('paging') or {}).get('next') or {}).get('after')
        if not after:
            break
    return tagged


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
        try:
            t = conference_tag_sweep()
            if t:
                print(f'[conf-tag] stamped conference_source on {t} meeting(s)')
        except Exception as e:
            print(f'[conf-tag] error: {e}')
        time.sleep(300)


def sheet_reconcile_loop():
    """Run scripts/sheet_reconcile.py at most ONCE per UTC day, backfilling
    Apollo enrichment for any meeting the inline write missed phones/emails on.

    CRITICAL: periodic_restart() exits the process every 30 min, so a plain
    `time.sleep(24*3600)` never survives to fire — the reconcile would re-run
    on every boot (~48x/day), re-scanning 14 days of meetings each time and
    flooding Ellen's sheet with writes + duplicate appends. We gate on a
    date-stamped marker file that persists across the in-process restarts
    (same container fs); a real redeploy wipes it and we run once more, which
    is harmless. Sleeps 60s on boot to let everything else settle first."""
    import subprocess
    from datetime import datetime, timezone
    time.sleep(60)
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.last_sheet_reconcile')
    while True:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        last = ''
        try:
            if os.path.exists(marker):
                with open(marker) as f:
                    last = f.read().strip()
        except Exception:
            pass
        if last == today:
            print(f'[sheet-reconcile] already ran on {today} — skipping (restart-safe)')
        else:
            try:
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts', 'sheet_reconcile.py')
                r = subprocess.run(['python3', script, '--since', '14'], capture_output=True, timeout=900)
                print(f'[sheet-reconcile] exit={r.returncode}')
                for line in (r.stdout.decode('utf-8', 'replace').splitlines()[-5:]
                             + r.stderr.decode('utf-8', 'replace').splitlines()[-3:]):
                    print(f'[sheet-reconcile] {line}')
                # Only stamp the marker on a clean run, so a crash retries next loop.
                if r.returncode == 0:
                    with open(marker, 'w') as f:
                        f.write(today)
            except Exception as e:
                print(f'[sheet-reconcile] error: {e}')
        # Re-check hourly. With periodic_restart this loop rarely lives an hour,
        # but the marker makes every boot a no-op until the UTC day rolls over.
        time.sleep(3600)


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
                    # Blitz channel is owned by conference_buddy: never process its posts as bookings
                    if AE_BLITZ_CHANNEL_ID and cid == AE_BLITZ_CHANNEL_ID:
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
                    bookings = [b for b in bookings if b and b.get('is_booking') and (b.get('contact_first_name') or b.get('contact_last_name') or b.get('contact_email') or b.get('company_name'))]  # require a person or company (drop chatter)
                    if not bookings:
                        continue
                    owner_id = slack_user_to_owner(app.client, user_id)
                    _random_react(app.client, cid, ts, count=3)
                    silent_say = lambda **kw: None
                    for parsed in bookings:
                        try:
                            _process_booking(parsed, text, owner_id, ts, app.client, silent_say, channel=cid)
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
    threading.Thread(target=exec_attach_loop, daemon=True).start()
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
    if CREDIT_BY_CALENDAR:
        def _credit_retry_ctx(ctx):                   # deal_id is already carried in ctx
            return calendar_credit.credit_after_booking(
                meeting_id=ctx['meeting_id'], deal_id=ctx['deal_id'],
                deal_owner_id=ctx.get('deal_owner_id', ''), incumbent_ae=ctx.get('incumbent_ae'),
                booker_email=ctx['booker_email'], prospect_email=ctx.get('prospect_email'),
                external_url=ctx.get('external_url'), start_iso=ctx.get('start_iso'),
                ae_email_map=_ae_email_map(), owner_name_fn=_owner_name,
                assign_enabled=CREDIT_ASSIGN_ENABLED, assign_fn=_hs_set_deal_owner)

        def _credit_retry_loop():
            while True:
                time.sleep(120)                       # every 2 min, within RETRY_TTL
                try:
                    calendar_credit.run_retry_once(credit_fn=_credit_retry_ctx)
                except Exception as e:
                    print(f'[credit-retry] loop error: {e}', flush=True)

        threading.Thread(target=_credit_retry_loop, daemon=True).start()
    threading.Thread(target=recycle_dry_run_once, daemon=True).start()
    if os.environ.get('RECYCLE_DRY_RUN') == '1':
        print('[recycle-dryrun] READ-ONLY preview scheduled (no DMs, no writes)')
    threading.Thread(target=recycle_loop, daemon=True).start()
    print('[recycle] warn(Thu)/release(Mon) loop started — '
          + ('LIVE' if os.environ.get('RECYCLE_ENABLED') == '1' else 'dry-run (RECYCLE_ENABLED!=1)'))
    handler.start()
