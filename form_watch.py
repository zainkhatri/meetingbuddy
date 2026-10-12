"""Conference landing-page meeting forms → #conference-meetings + Full Meeting Tracker.

Prospects who request a meeting through a conference landing-page form (e.g.
"WSIA 2026 Meeting Form") never produce a BDR booking post, so meetingbuddy never
saw them and the tracker missed them (Arianna, 2026-10-12).

Every FORM_WATCH_POLL_SECS this:
  1. reads submissions on every form named "... Meeting Form" (Forms API; the
     meetingbuddy token has forms scope), skipping internal/test entries,
  2. posts each new submission once to #conference-meetings, @-ing the contact's
     HubSpot owner (AE). Dedup key `form-sub:<contact>:<ts>` lives in the post
     itself, so it survives redeploys,
  3. upserts a tracker row (Meeting Sourced By = "Marketing (form)", status
     "Requested"), then fills date/time/status once a HubSpot meeting with that
     contact shows up.

Pure helpers are unit-tested in tests/test_form_watch.py.
"""
import os
import re
import time
from datetime import datetime, timezone

import requests

import sheet_sync

FORM_PATTERN = re.compile(os.environ.get('FORM_WATCH_PATTERN', r'meeting form\s*$'), re.I)
LOOKBACK_DAYS = int(os.environ.get('FORM_WATCH_LOOKBACK_DAYS', '14'))
SOURCED_BY = 'Marketing (form)'
MAX_CONTACTS = 200          # upper bound on submissions handled per poll
MAX_FORM_PAGES = 10         # 100 forms/page
MAX_SUB_PAGES = 10          # 50 submissions/page, per form
INTERNAL_DOMAINS = ('@furtherai.com', '@further.ai')
KEY_RE = re.compile(r'form-sub:\d+:[0-9T:\-]+')
MAX_MEETINGS = 50           # upper bound on meetings inspected per contact
HISTORY_LIMIT = 1000        # channel messages scanned for dedup keys
CONTACT_PROPS = ['firstname', 'lastname', 'email', 'company', 'jobtitle',
                 'hubspot_owner_id', 'recent_conversion_event_name',
                 'recent_conversion_date', 'associatedcompanyid']


def dedup_key(contact_id, conversion_iso):
    assert contact_id, 'contact_id required'
    assert conversion_iso, 'conversion_iso required'
    return f'form-sub:{contact_id}:{conversion_iso[:19]}'


def is_meeting_form(event_name):
    assert event_name is None or isinstance(event_name, str)
    return bool(event_name and FORM_PATTERN.search(event_name))


def pick_meeting(meetings, submitted_ms):
    """Earliest non-cancelled meeting starting after the submission (minus 1 day,
    for a meeting booked just before the form). None if there isn't one."""
    assert isinstance(meetings, list)
    assert isinstance(submitted_ms, int)
    best = None
    for m in meetings[:MAX_MEETINGS]:
        p = m.get('properties') or {}
        if (p.get('hs_meeting_outcome') or '').upper() in ('CANCELED', 'CANCELLED', 'NO_SHOW'):
            continue
        dt = sheet_sync._to_dt(p.get('hs_meeting_start_time'))
        if not dt:
            continue
        start_ms = int(dt.timestamp() * 1000)
        if start_ms < submitted_ms - 86400 * 1000:
            continue
        if best is None or start_ms < best[0]:
            best = (start_ms, m)
    return best[1] if best else None


def build_row(*, conference, contact, ae_name, form_name, meeting):
    """Tracker payload for a form submission; meeting may be None."""
    assert conference, 'conference display name required'
    assert isinstance(contact, dict)
    mp = (meeting or {}).get('properties') or {}
    start = mp.get('hs_meeting_start_time')
    submitted = sheet_sync._fmt_date(contact.get('recent_conversion_date'))
    name = ' '.join(filter(None, [(contact.get('firstname') or '').strip(),
                                  (contact.get('lastname') or '').strip()]))
    status = sheet_sync.STATUS_MAP.get((mp.get('hs_meeting_outcome') or 'SCHEDULED').upper(), '') \
        if meeting else 'Requested'
    return {
        'Conference': conference,
        'Meeting Sourced By': SOURCED_BY,
        'Account Executive': ae_name or '',
        'Meeting Date': sheet_sync._fmt_date(start) if start else '',
        'Meeting Time': sheet_sync._fmt_time(start) if start else '',
        'Prospect Company': (contact.get('company') or '').strip(),
        'Prospect Name': name,
        'Prospect Title': (contact.get('jobtitle') or '').strip(),
        'Prospect Email': (contact.get('email') or '').strip(),
        'Meeting Status': status,
        'Notes': f'Inbound via {form_name} ({submitted})',
    }


def find_form_row(rows, headers, payload):
    """1-based row of this prospect's form row for this conference, else None.
    Matched on (Conference, Prospect Email or Prospect Name) — not the company/date
    key, because a form row starts with no date."""
    assert isinstance(rows, list)
    assert isinstance(headers, dict)
    conf_c, em_c, nm_c = (headers.get('Conference'), headers.get('Prospect Email'),
                          headers.get('Prospect Name'))
    if not (conf_c and em_c and nm_c):
        return None
    want_conf = payload['Conference'].strip().lower()
    want_em = payload['Prospect Email'].strip().lower()
    want_nm = payload['Prospect Name'].strip().lower()
    for i in range(1, len(rows)):
        row = rows[i] + [''] * max(0, max(conf_c, em_c, nm_c) - len(rows[i]))
        if row[conf_c - 1].strip().lower() != want_conf:
            continue
        if want_em and row[em_c - 1].strip().lower() == want_em:
            return i + 1
        if want_nm and row[nm_c - 1].strip().lower() == want_nm:
            return i + 1
    return None


def merge_form_row(header_row, current, payload, last_col):
    """Like sheet_sync._merge_existing_row, but date/time/status also overwrite
    once a meeting exists (a 'Requested' row graduates to 'Scheduled')."""
    assert isinstance(header_row, list)
    assert isinstance(current, list)
    owned = ('Conference', 'Meeting Status', 'Meeting Date', 'Meeting Time')
    merged, changed = [], False
    for i, h in enumerate(header_row[:last_col]):
        key = h.strip()
        new = payload.get(key, '')
        old = current[i] if i < len(current) else ''
        if key in owned and new and new != old:
            merged.append(new)
            changed = True
        elif old.strip():
            merged.append(old)
        else:
            merged.append(new)
            changed = changed or bool(new)
    return merged, changed


def slack_text(*, conference, contact, form_name, ae_mention, meeting, key):
    assert conference and key
    assert isinstance(contact, dict)
    name = ' '.join(filter(None, [contact.get('firstname'), contact.get('lastname')])) or 'Unknown'
    who = name + (f" – {contact['jobtitle']}" if contact.get('jobtitle') else '')
    who += f" @ {contact['company']}" if contact.get('company') else ''
    lines = [f':inbox_tray: *{conference} meeting request (landing-page form)*', who]
    if contact.get('email'):
        lines.append(contact['email'])
    mp = (meeting or {}).get('properties') or {}
    if meeting:
        when = f"{sheet_sync._fmt_date(mp.get('hs_meeting_start_time'))} " \
               f"{sheet_sync._fmt_time(mp.get('hs_meeting_start_time'))}"
        lines.append(f":calendar: On the calendar: {mp.get('hs_meeting_title') or 'meeting'} – {when}")
    else:
        lines.append(f':warning: No meeting on the calendar yet. {ae_mention or "AE"} please reach out to schedule.')
    lines.append(f'_{form_name} · added to the meeting tracker · {key}_')
    return '\n'.join(lines)


# ---------- I/O ----------

def meeting_forms(hs):
    """[(form_id, form_name)] for every live form whose name matches FORM_PATTERN."""
    assert 'Authorization' in hs
    out, after = [], None
    for _ in range(MAX_FORM_PAGES):
        params = {'limit': 100, **({'after': after} if after else {})}
        r = requests.get('https://api.hubapi.com/marketing/v3/forms', headers=hs, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out += [(f['id'], f['name']) for f in data.get('results', [])
                if not f.get('archived') and is_meeting_form(f.get('name'))]
        after = ((data.get('paging') or {}).get('next') or {}).get('after')
        if not after:
            break
    return out


def is_test_submission(values):
    """Internal teammates and obvious QA entries (ITC's form got ~10 on 9/2-9/14)."""
    assert isinstance(values, dict)
    email = (values.get('email') or '').lower()
    if email.endswith(INTERNAL_DOMAINS) or re.search(r'@test|testing', email):
        return True
    first = (values.get('firstname') or '').strip().lower()
    return bool(re.fullmatch(r'test\w*', first))


def form_submissions(hs, form_id, since_ms):
    """Submissions on one form at/after since_ms, newest first (API order)."""
    assert form_id
    out, after = [], None
    for _ in range(MAX_SUB_PAGES):
        params = {'limit': 50, **({'after': after} if after else {})}
        r = requests.get(f'https://api.hubapi.com/form-integrations/v1/submissions/forms/{form_id}',
                         headers=hs, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for s in data.get('results', []):
            if s['submittedAt'] < since_ms:
                return out
            out.append(s)
        after = ((data.get('paging') or {}).get('next') or {}).get('after')
        if not after:
            break
    return out


def contact_by_email(hs, email):
    assert email
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts/search', headers=hs, timeout=15,
                      json={'filterGroups': [{'filters': [
                          {'propertyName': 'email', 'operator': 'EQ', 'value': email}]}],
                          'properties': CONTACT_PROPS, 'limit': 1})
    res = r.json().get('results', []) if r.ok else []
    return res[0] if res else None


def search_submissions(hs, now):
    """Real (non-test) meeting-form submissions in the lookback, as contact records
    whose properties carry the submitted values plus the form name/time."""
    since_ms = int((now - LOOKBACK_DAYS * 86400) * 1000)
    out = []
    for form_id, form_name in meeting_forms(hs):
        for s in form_submissions(hs, form_id, since_ms):
            vals = {v['name']: v['value'] for v in s.get('values', [])}
            if is_test_submission(vals) or not vals.get('email') or len(out) >= MAX_CONTACTS:
                continue
            c = contact_by_email(hs, vals['email'])
            if not c:
                continue
            props = dict(c.get('properties') or {})
            props.update({k: v for k, v in vals.items() if k in CONTACT_PROPS and v})
            props['recent_conversion_event_name'] = form_name
            props['recent_conversion_date'] = datetime.fromtimestamp(
                s['submittedAt'] / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            out.append({'id': c['id'], 'properties': props})
    return out


def contact_meetings(hs, contact_id):
    assert contact_id
    r = requests.get(f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/meetings',
                     headers=hs, timeout=15)
    if r.status_code != 200:
        return []
    ids = [str(a['toObjectId']) for a in r.json().get('results', [])][:MAX_MEETINGS]
    if not ids:
        return []
    r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/batch/read', headers=hs, timeout=15,
                      json={'inputs': [{'id': i} for i in ids],
                            'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'hs_meeting_outcome']})
    return r.json().get('results', []) if r.ok else []


def owner_info(hs, owner_id):
    """(first name, full name) for a HubSpot owner, ('', '') on failure."""
    if not owner_id:
        return '', ''
    r = requests.get(f'https://api.hubapi.com/crm/v3/owners/{owner_id}', headers=hs, timeout=15)
    if r.status_code != 200:
        return '', ''
    d = r.json()
    first = (d.get('firstName') or '').strip()
    return first, ' '.join(filter(None, [first, (d.get('lastName') or '').strip()]))


def slack_id_by_name(slack, full_name):
    """Exact real_name match (the bot token lacks users:read.email)."""
    if not full_name:
        return None
    resp = slack.users_list(limit=500)
    want = full_name.strip().lower()
    for u in resp.get('members', []):
        if not u.get('deleted') and (u.get('real_name') or '').strip().lower() == want:
            return u['id']
    return None


_POSTED = set()  # keys this process has posted — survives a failed history read


def posted_keys(slack, channel, since_ts):
    """(dedup keys already in the channel since `since_ts`, messages scanned).
    No `oldest` param: Slack intermittently returns zero messages with it, which
    made every poll repost (2026-10-12). Page newest-first and stop at since_ts."""
    keys, cursor, scanned = set(), None, 0
    for _ in range(HISTORY_LIMIT // 200):
        kw = {'channel': channel, 'limit': 200, **({'cursor': cursor} if cursor else {})}
        resp = slack.conversations_history(**kw)
        msgs = resp.get('messages', [])
        scanned += len(msgs)
        for m in msgs:
            keys.update(KEY_RE.findall(m.get('text') or ''))
        if not msgs or float(msgs[-1]['ts']) < since_ts:
            break
        cursor = (resp.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
    return keys, scanned


def upsert_form_row(payload):
    ws = sheet_sync._connect()
    if ws is None:
        return 'unconfigured'
    rows = sheet_sync._retry(ws.get_all_values)
    header_row = rows[0] if rows else []
    last_col = max((i + 1 for i, h in enumerate(header_row) if h.strip()), default=len(header_row))
    headers = {h.strip(): i + 1 for i, h in enumerate(header_row) if h.strip()}
    row_num = find_form_row(rows, headers, payload)
    if row_num is None:
        values = [payload.get(h.strip(), '') for h in header_row][:last_col]
        sheet_sync._retry(ws.append_row, values, value_input_option='USER_ENTERED', table_range='A1')
        return 'inserted'
    merged, changed = merge_form_row(header_row, rows[row_num - 1], payload, last_col)
    if not changed:
        return 'noop'
    rng = f'A{row_num}:{sheet_sync._col_letter(last_col)}{row_num}'
    sheet_sync._retry(ws.update, rng, [merged], value_input_option='USER_ENTERED')
    return 'updated'


def run_once(*, hs, slack, channel, detect_conference, now=None, post=True):
    """One poll. detect_conference: form name → conference slug (meeting_bot's
    title rules). Returns counts for logging."""
    now = now or time.time()
    counts = {'seen': 0, 'posted': 0, 'rows': 0, 'skipped': 0}
    subs = search_submissions(hs, now)
    seen_keys, scanned = posted_keys(slack, channel, now - (LOOKBACK_DAYS + 1) * 86400) if subs else (set(), 0)
    seen_keys |= _POSTED
    if subs and scanned == 0:
        post = False  # can't prove what's already posted — never guess, never spam
        counts['skipped'] += len(subs)
    for c in subs[:MAX_CONTACTS]:
        counts['seen'] += 1
        p = c.get('properties') or {}
        form_name = p.get('recent_conversion_event_name') or ''
        slug = detect_conference(form_name)
        conference = sheet_sync.CONFERENCE_DISPLAY.get(slug or '', '') or \
            re.sub(r'\s*meeting form\s*$', '', form_name, flags=re.I).strip()
        sub_dt = sheet_sync._to_dt(p.get('recent_conversion_date'))
        if not (conference and sub_dt):
            counts['skipped'] += 1
            continue
        meeting = pick_meeting(contact_meetings(hs, c['id']), int(sub_dt.timestamp() * 1000))
        ae_first, ae_full = owner_info(hs, p.get('hubspot_owner_id'))
        payload = build_row(conference=conference, contact=p, ae_name=ae_first,
                            form_name=form_name, meeting=meeting)
        action = upsert_form_row(payload)
        counts['rows'] += action in ('inserted', 'updated')
        key = dedup_key(c['id'], sub_dt.strftime('%Y-%m-%dT%H:%M:%S'))
        if post and key not in seen_keys:
            uid = slack_id_by_name(slack, ae_full)
            text = slack_text(conference=conference, contact=p, form_name=form_name,
                              ae_mention=f'<@{uid}>' if uid else ae_first, meeting=meeting, key=key)
            slack.chat_postMessage(channel=channel, text=text, unfurl_links=False)
            seen_keys.add(key)
            _POSTED.add(key)
            counts['posted'] += 1
    return counts
