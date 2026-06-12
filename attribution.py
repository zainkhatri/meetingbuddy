"""Collision-safe contact + meeting matching for the meeting bot.

Extracted from meeting_bot so it can be unit-tested without constructing the
Slack app. The whole point is the failure mode that caused real misattribution:
two different people share a name (e.g. "Dave Rose @ Kentucky Farm Bureau" and
"Dave Rose @ bolt"), the old matcher took the first hit, and a booking got
stamped onto the WRONG person's meeting — even another BDR's.

Rules enforced here:
  1. Email is collision-free -> it wins outright.
  2. Otherwise match exact first+last name; if several people share it,
     disambiguate by the COMPANY named in the post.
  3. If still ambiguous, return None — never guess. The caller then creates a
     fresh contact / matches the meeting by company+date, which can't steal
     someone else's record.
  4. A meeting may only be tagged if its title matches the posted company
     (title_matches_company) — the hard guard against cross-company tagging.

Only depends on `requests` + stdlib, so tests can mock `requests.post`.
"""
import re

import requests

CONTACT_SEARCH_URL = 'https://api.hubapi.com/crm/v3/objects/contacts/search'

# Generic words that carry no identifying signal for a company name.
COMPANY_STOP = {
    'insurance', 'insurer', 'insurers', 'group', 'inc', 'llc', 'ltd', 'co',
    'company', 'companies', 'corporation', 'corp', 'mutual', 'the', 'and', 'of',
    'partners', 'holdings', 'services', 'solutions', 'national', 'specialty',
    'underwriting', 'underwriters', 're', 'us', 'usa', 'furtherai', 'demo', 'meeting',
}

_TOKEN_RE = re.compile(r"[a-z0-9&']+")


def company_tokens(name):
    """Identifying lowercase tokens of a company name (stopwords removed)."""
    if not name:
        return set()
    return {t for t in _TOKEN_RE.findall(name.lower())
            if t not in COMPANY_STOP and len(t) > 1}


def title_matches_company(title, company):
    """True if a meeting title shares an identifying token with the posted
    company. The hard guard: never tag a meeting whose company doesn't match the
    post. Returns True when the company can't be tokenized (can't verify ->
    don't block — the contact/email match is then the safety net)."""
    ct = company_tokens(company)
    if not ct:
        return True
    return bool(ct & set(_TOKEN_RE.findall((title or '').lower())))


def _search(hs_headers, filters, limit):
    body = {'filterGroups': [{'filters': filters}],
            'properties': ['firstname', 'lastname', 'email', 'company'],
            'limit': limit}
    r = requests.post(CONTACT_SEARCH_URL, headers=hs_headers, json=body, timeout=30)
    try:
        return r.json().get('results', []) if r.status_code == 200 else []
    except Exception:
        return []


def find_contact(hs_headers, first, last, email=None, company_name=None):
    """Resolve the posted person to ONE HubSpot contact, collision-safe.

    Returns the contact dict ({'id', 'properties', ...}) or None. None means
    "couldn't safely identify a unique person" — the caller should NOT fall back
    to a name-only guess; create a fresh contact or match by company+date."""
    # 1. Email — unambiguous, wins outright.
    if email:
        rs = _search(hs_headers, [{'propertyName': 'email', 'operator': 'EQ',
                                   'value': email.lower()}], 1)
        if rs:
            return rs[0]
    # 2. Exact first+last name.
    filters = []
    if first:
        filters.append({'propertyName': 'firstname', 'operator': 'EQ', 'value': first})
    if last:
        filters.append({'propertyName': 'lastname', 'operator': 'EQ', 'value': last})
    if not filters:
        return None
    rs = _search(hs_headers, filters, 10)
    if not rs:
        return None
    if len(rs) == 1:
        return rs[0]
    # 3. Several people share this name -> disambiguate by company token overlap.
    ct = company_tokens(company_name)
    if ct:
        matches = [c for c in rs
                   if ct & company_tokens((c.get('properties') or {}).get('company'))]
        if len(matches) == 1:
            return matches[0]
    # 4. Still ambiguous -> refuse to guess.
    return None
