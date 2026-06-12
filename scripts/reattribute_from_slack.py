#!/usr/bin/env python3
"""Recompute meeting attribution from Slack truth — for ALL BDRs in #bdr-team.

The Slack post is the source of truth. It tells us two things directly:
  1. WHO sourced it  -> the person who posted the message.
  2. WHICH meeting    -> the COMPANY + DATE + contact named in the post.

The live bot's bug is that it identifies the meeting by pivoting through a
first+last-name contact lookup (`hs_find_contact`) with no company check, so
common names collide ("Dave Rose @ Kentucky Farm Bureau" matched a "Dave Rose @
bolt") and it stamps the WRONG meeting — even one a different BDR sourced.

This tool fixes that with a company-anchored matcher and one hard guard:
  *** Never tag a meeting whose title doesn't match the company in the post. ***
That guard alone makes the bolt-style cross-BDR misattribution impossible.

It then compares each meeting's current `meeting_sourced_by` to the rightful
poster and reports:
  - WRONG  : currently credited to someone else  -> fix to the poster
  - MISSING: currently uncredited                -> set to the poster
  - OK     : already correct
  - NO-MATCH: no company-consistent meeting found -> left untouched, reported

Dry run by default. --execute to write. Tokens from env (SLACK_BOT_TOKEN,
ANTHROPIC_API_KEY, HS_API_KEY).
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
PARSE_MODEL = 'claude-haiku-4-5-20251001'

# Full BDR roster — last-name (or unambiguous first-name) substring match on the
# poster's Slack real_name. Mirrors meeting_bot.NAME_TO_OWNER.
NAME_TO_OWNER = {
    'zain': '88760040', 'khatri': '88760040',
    'jacob': '162210484', 'sanders': '162210484',
    'dani': '82377567', 'daniella': '82377567', 'salgado': '82377567',
    'trotter': '164943105',   # Ben Trotter
    'stapleton': '92184259',  # Matt Stapleton
}
OWNER_NAME = {'88760040': 'Zain', '162210484': 'Jacob', '82377567': 'Dani',
              '164943105': 'Ben Trotter', '92184259': 'Matt Stapleton'}

# Words that carry no identifying signal for a company — dropped before matching.
_STOP = {'insurance', 'insurer', 'insurers', 'group', 'inc', 'llc', 'ltd', 'co',
         'company', 'companies', 'corporation', 'corp', 'mutual', 'the', 'and',
         'of', 'partners', 'holdings', 'services', 'solutions', 'national',
         'specialty', 'underwriting', 'underwriters', 're', 'us', 'usa',
         'furtherai', 'demo', 'meeting'}

def _load_parse_prompt():
    """Reuse the live bot's PARSE_PROMPT verbatim by reading meeting_bot.py."""
    botfile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'meeting_bot.py')
    src = open(botfile).read()
    m = re.search(r'PARSE_PROMPT\s*=\s*"""(.*?)"""', src, re.S)
    assert m, 'could not extract PARSE_PROMPT from meeting_bot.py'
    return m.group(1)


PARSE_PROMPT = _load_parse_prompt()


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
        # Tolerant decode: Claude occasionally appends prose/extra objects after
        # the JSON. Grab the first complete JSON value and ignore the rest.
        start = next((i for i, ch in enumerate(txt) if ch in '{['), None)
        if start is None:
            return None
        return json.JSONDecoder().raw_decode(txt[start:])[0]
    except Exception as e:
        print(f'  [claude parse error] {e}', flush=True)
        return None


def company_tokens(name):
    """Significant lowercase tokens of a company name (stopwords removed)."""
    if not name:
        return set()
    toks = re.findall(r"[a-z0-9&']+", name.lower())
    return {t for t in toks if t not in _STOP and len(t) > 1}


def title_matches_company(title, company):
    """True if the meeting title shares an identifying token with the posted
    company. This is THE guard against cross-company/cross-BDR misattribution."""
    ct = company_tokens(company)
    if not ct:
        return False
    tt = set(re.findall(r"[a-z0-9&']+", (title or '').lower()))
    return bool(ct & tt)


# --- HubSpot ---
def hs_find_contact_by_email(email):
    if not email:
        return None
    body = {'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': email.lower()}]}],
            'properties': ['firstname', 'lastname', 'email'], 'limit': 1}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts/search', headers=HS, json=body, timeout=30)
    rs = r.json().get('results', [])
    return rs[0] if rs else None


def meetings_for_contact(contact_id):
    r = requests.get(f'https://api.hubapi.com/crm/v4/objects/contacts/{contact_id}/associations/meetings',
                     headers=HS, timeout=15)
    return [str(a['toObjectId']) for a in r.json().get('results', [])] if r.status_code == 200 else []


def meeting_props(mid):
    r = requests.get(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}', headers=HS,
                     params={'properties': 'hs_meeting_title,hs_meeting_start_time,meeting_sourced_by,hs_meeting_outcome,hubspot_owner_id'},
                     timeout=10)
    return r.json().get('properties', {}) if r.status_code == 200 else None


def search_meetings_by_company(company, lo_ms, hi_ms):
    """Meetings whose title contains a company token, in the date window. Tries
    the full name then the single most-distinctive token to widen the net."""
    out = {}
    toks = sorted(company_tokens(company), key=len, reverse=True)
    queries = [company] + toks[:2]
    for q in queries:
        if not q:
            continue
        body = {'filterGroups': [{'filters': [
            {'propertyName': 'hs_meeting_title', 'operator': 'CONTAINS_TOKEN', 'value': q},
            {'propertyName': 'hs_meeting_start_time', 'operator': 'BETWEEN', 'value': str(lo_ms), 'highValue': str(hi_ms)},
        ]}], 'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'meeting_sourced_by', 'hs_meeting_outcome', 'hubspot_owner_id'], 'limit': 25}
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search', headers=HS, json=body, timeout=30)
        if r.status_code == 200:
            for m in r.json().get('results', []):
                out[m['id']] = m.get('properties', {})
    return out


def _alive(p):
    if (p.get('hs_meeting_outcome') or '') in ('CANCELED', 'NO_SHOW'):
        return False
    if (p.get('hs_meeting_title') or '').lower().startswith('canceled:'):
        return False
    return True


def find_contacts_by_name(first, last):
    """All contacts with this exact first+last name (the collision set)."""
    filters = []
    if first:
        filters.append({'propertyName': 'firstname', 'operator': 'EQ', 'value': first})
    if last:
        filters.append({'propertyName': 'lastname', 'operator': 'EQ', 'value': last})
    if not filters:
        return []
    body = {'filterGroups': [{'filters': filters}],
            'properties': ['firstname', 'lastname', 'email', 'company'], 'limit': 10}
    r = requests.post('https://api.hubapi.com/crm/v3/objects/contacts/search', headers=HS, json=body, timeout=30)
    return r.json().get('results', []) if r.status_code == 200 else []


def pick_contact(parsed):
    """Resolve the posted person to ONE HubSpot contact, anchored on their NAME
    and disambiguated by the posted COMPANY (the Dave Rose @ bolt vs @ KFB case).
    Email wins outright when present. Returns (contact_id, reason)."""
    email = parsed.get('contact_email')
    if email:
        c = hs_find_contact_by_email(email)
        if c:
            return c['id'], 'email'
    first, last = parsed.get('contact_first_name'), parsed.get('contact_last_name')
    if not (first or last):
        return None, 'no_contact_in_post'
    cands = find_contacts_by_name(first, last)
    if not cands:
        return None, 'contact_not_found'
    if len(cands) == 1:
        return cands[0]['id'], 'unique_name'
    # Multiple same-named contacts -> disambiguate by company token overlap.
    ct = company_tokens(parsed.get('company_name'))
    matches = [c for c in cands
               if ct and (ct & set(re.findall(r"[a-z0-9&']+", (c['properties'].get('company') or '').lower())))]
    if len(matches) == 1:
        return matches[0]['id'], 'name+company'
    return None, 'ambiguous_contact'  # can't safely pick -> leave for manual


def resolve_meeting(parsed):
    """Contact-anchored meeting resolution. Returns (mid, props) or (None, reason).

    The posted CONTACT NAME is the unique key (Slack truth). We resolve it to one
    contact (company breaks same-name ties), then take that contact's meeting
    nearest the posted date. The company token is NEVER used to pull in meetings
    on its own — that's what mis-grabbed Liberty Mutual / Gallagher-Freddie /
    Nationwide-Sara. No date in the post -> can't place it."""
    date_str = parsed.get('meeting_date')
    if not date_str:
        return None, 'no_date_in_post'
    try:
        target = datetime.fromisoformat(f'{date_str}T12:00:00+00:00')
    except Exception:
        return None, 'bad_date'

    contact_id, reason = pick_contact(parsed)
    if not contact_id:
        return None, reason

    scored = []
    for mid in meetings_for_contact(contact_id):
        p = meeting_props(mid)
        if not p or not _alive(p):
            continue
        start = p.get('hs_meeting_start_time')
        try:
            sdt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except Exception:
            continue
        diff = abs((sdt - target).total_seconds())
        if diff > 5 * 86400:
            continue
        scored.append((diff, mid, p))
    if not scored:
        return None, 'no_meeting_near_date'
    scored.sort()
    _, mid, p = scored[0]
    return mid, p


_uname = {}


def owner_for_user(uid):
    if uid in _uname:
        return _uname[uid]
    d = requests.get('https://slack.com/api/users.info', headers=SLK, params={'user': uid}, timeout=15).json()
    info = d.get('user') or {}
    display = (info.get('real_name') or info.get('name') or '').lower()
    res = None
    for k, oid in NAME_TO_OWNER.items():
        if k in display:
            res = oid
            break
    _uname[uid] = res
    return res


def slack_get(method, params):
    for _ in range(5):
        r = requests.get(f'https://slack.com/api/{method}', headers=SLK, params=params, timeout=30)
        d = r.json()
        if d.get('error') == 'ratelimited':
            time.sleep(int(r.headers.get('Retry-After', 2)))
            continue
        return d
    return {}


def full_history(cid):
    parents, cursor = [], None
    for _ in range(200):
        params = {'channel': cid, 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        d = slack_get('conversations.history', params)
        if not d.get('ok'):
            break
        parents.extend(d.get('messages', []) or [])
        cursor = (d.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
        time.sleep(0.4)
    seen, out = set(), []
    for m in parents:
        if m.get('ts') not in seen:
            seen.add(m.get('ts'))
            out.append(m)
        if m.get('reply_count'):
            cur2 = None
            for _ in range(20):
                pp = {'channel': cid, 'ts': m['ts'], 'limit': 200}
                if cur2:
                    pp['cursor'] = cur2
                dd = slack_get('conversations.replies', pp)
                for r in dd.get('messages', []) or []:
                    if r.get('ts') not in seen:
                        seen.add(r.get('ts'))
                        out.append(r)
                cur2 = (dd.get('response_metadata') or {}).get('next_cursor')
                if not cur2:
                    break
                time.sleep(0.3)
            time.sleep(0.2)
    out.sort(key=lambda m: float(m.get('ts', 0)))
    return out


def post_date(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime('%Y-%m-%d')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--since', help='YYYY-MM-DD')
    args = ap.parse_args()
    since_ts = datetime.fromisoformat(args.since + 'T00:00:00+00:00').timestamp() if args.since else 0.0

    mode = 'EXECUTE' if args.execute else 'DRY RUN'
    print(f'=== Re-attribute meetings from Slack truth [{mode}] ===', flush=True)

    chans = slack_get('users.conversations', {'types': 'public_channel,private_channel', 'limit': 200}).get('channels', [])
    cid = next((c['id'] for c in chans if c.get('name') == 'bdr-team'), None)
    assert cid, '#bdr-team not found / bot not a member'
    msgs = full_history(cid)
    print(f'#bdr-team: {len(msgs)} messages (incl. thread replies)', flush=True)

    counts = Counter()
    nomatch = []
    # Phase 1: resolve every booking post to a meeting + intended poster.
    resolutions = {}  # mid -> list of dicts
    for m in msgs:
        if m.get('bot_id') or m.get('subtype'):
            continue
        ts, uid = m.get('ts'), m.get('user')
        text = (m.get('text') or '').strip()
        if not ts or not uid or not text or float(ts) < since_ts:
            continue
        poster = owner_for_user(uid)
        if not poster:
            continue
        counts['roster_msgs'] += 1
        parsed_raw = parse_with_claude(text, post_date(ts))
        if not parsed_raw:
            continue
        for parsed in (parsed_raw if isinstance(parsed_raw, list) else [parsed_raw]):
            if not parsed or not parsed.get('is_booking'):
                continue
            counts['bookings'] += 1
            who = OWNER_NAME[poster]
            co = parsed.get('company_name') or '?'
            label = f"{parsed.get('contact_first_name') or ''} {parsed.get('contact_last_name') or ''}".strip()
            mid, p = resolve_meeting(parsed)
            if not mid:
                counts['no_match'] += 1
                nomatch.append((who, post_date(ts), label, co, parsed.get('meeting_date'), p))
                continue
            resolutions.setdefault(mid, []).append(
                {'poster': poster, 'who': who, 'co': co, 'date': parsed.get('meeting_date'),
                 'cur': (p.get('meeting_sourced_by') or '').strip(),
                 'owner': (p.get('hubspot_owner_id') or '').strip(),
                 'title': (p.get('hs_meeting_title') or '')[:42]})

    # Phase 2: classify. A meeting claimed by >1 DISTINCT poster is ambiguous
    # (conference name-collision) — never auto-fix; report for manual review.
    wrong, missing, ambiguous = [], [], []
    for mid, rs in resolutions.items():
        posters = {r['poster'] for r in rs}
        r0 = rs[0]
        if len(posters) > 1:
            counts['ambiguous'] += 1
            ambiguous.append((mid, r0['title'], sorted({r['who'] for r in rs}), r0['cur']))
            continue
        poster = next(iter(posters))
        if r0['cur'] == poster:
            counts['ok'] += 1
            continue
        row = (r0['who'], mid, OWNER_NAME.get(r0['cur'], r0['cur'] or '∅'), r0['title'], r0['co'], r0['date'])
        (missing if not r0['cur'] else wrong).append(row)
        counts['missing' if not r0['cur'] else 'wrong'] += 1
        if args.execute:
            props = {'meeting_sourced_by': poster}
            if not r0['owner']:
                props['hubspot_owner_id'] = poster
            rr = requests.patch(f'https://api.hubapi.com/crm/v3/objects/meetings/{mid}',
                                headers=HS, json={'properties': props}, timeout=30)
            counts['patched' if rr.status_code == 200 else 'patch_fail'] += 1

    print(f'\n===== RESULT [{mode}] =====\n{dict(counts)}', flush=True)
    if wrong:
        print(f'\nWRONG — credited to the wrong BDR, single clear claimant ({len(wrong)}):', flush=True)
        for who, mid, cur, title, co, d in wrong:
            print(f'  {mid}  {cur:>14} -> {who:<14} | {title} | {co} ({d})', flush=True)
    if missing:
        print(f'\nMISSING — uncredited, single clear claimant ({len(missing)}):', flush=True)
        for who, mid, cur, title, co, d in missing:
            print(f'  {mid}  ∅ -> {who:<14} | {title} | {co} ({d})', flush=True)
    if ambiguous:
        print(f'\nAMBIGUOUS — same meeting claimed by multiple BDRs, NOT touched ({len(ambiguous)}):', flush=True)
        for mid, title, whos, cur in ambiguous:
            print(f'  {mid}  claimants={whos} cur={OWNER_NAME.get(cur, cur or "∅")} | {title}', flush=True)
    if nomatch:
        print(f'\nNO COMPANY-CONSISTENT MEETING ({len(nomatch)}) — left untouched:', flush=True)
        for who, pd, label, co, d, reason in nomatch:
            print(f'  {who:<14} posted={pd} | {label} @ {co} ({d}) [{reason}]', flush=True)


if __name__ == '__main__':
    main()
