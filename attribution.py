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
  3. If still ambiguous, return None — never guess. The caller matches the
     meeting by company+date, which can't steal someone else's record, and
     does NOT create a fresh contact (lookup_contact flags this as ambiguous;
     a new same-name contact is how duplicates leaked in).
  4. A meeting may only be tagged if its title matches the posted company
     (title_matches_company) — the hard guard against cross-company tagging.

  5. A failed search (429/5xx after retries, bad JSON) RAISES. It must never
     read as "no match", or the caller creates a duplicate contact.

Only depends on `requests` + stdlib, so tests can mock `requests.post`.
"""
import re
import time

import requests

CONTACT_SEARCH_URL = 'https://api.hubapi.com/crm/v3/objects/contacts/search'
COMPANY_SEARCH_URL = 'https://api.hubapi.com/crm/v3/objects/companies/search'

SEARCH_RETRIES = 4
_RETRY_STATUS = {429, 500, 502, 503, 504}

# Free-mail domains never identify a company, so they're never a domain key.
FREE_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'outlook.com', 'hotmail.com',
    'icloud.com', 'aol.com', 'protonmail.com', 'proton.me', 'live.com',
    'msn.com', 'me.com', 'mac.com', 'comcast.net', 'att.net', 'ymail.com',
}

_LEGAL_SUFFIXES = {'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp',
                   'corporation', 'co', 'company', 'plc', 'lp', 'llp', 'pc'}

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


def hs_search(hs_headers, url, body):
    """POST a CRM search, retrying rate limits and 5xx. Raises when it still
    fails: a failed lookup must never read as "no match", or the caller
    creates a duplicate."""
    for attempt in range(SEARCH_RETRIES):
        last = attempt == SEARCH_RETRIES - 1
        try:
            r = requests.post(url, headers=hs_headers, json=body, timeout=30)
        except requests.RequestException:
            if last:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in _RETRY_STATUS and not last:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f'HubSpot search {r.status_code}: {url}')
        try:
            return r.json().get('results') or []
        except Exception as e:
            raise RuntimeError(f'HubSpot search bad JSON: {url}') from e
    raise RuntimeError(f'HubSpot search failed: {url}')


def _search(hs_headers, filters, limit):
    body = {'filterGroups': [{'filters': filters}],
            'properties': ['firstname', 'lastname', 'email', 'company'],
            'limit': limit}
    return hs_search(hs_headers, CONTACT_SEARCH_URL, body)


def find_contact(hs_headers, first, last, email=None, company_name=None):
    """Resolve the posted person to ONE HubSpot contact, collision-safe.

    Returns the contact dict ({'id', 'properties', ...}) or None. None means
    "couldn't safely identify a unique person" — the caller should NOT fall back
    to a name-only guess. Use lookup_contact to tell "nobody by this name"
    (safe to create) from "several, can't pick" (must not create)."""
    return lookup_contact(hs_headers, first, last, email, company_name)[0]


def lookup_contact(hs_headers, first, last, email=None, company_name=None):
    """Like find_contact, but returns (contact, ambiguous). ambiguous=True
    means same-name contacts exist and none could be uniquely picked: the
    caller must NOT create a new contact (that's a duplicate of one of them).
    Raises if HubSpot search fails."""
    # 1. Email — unambiguous, wins outright.
    if email:
        rs = _search(hs_headers, [{'propertyName': 'email', 'operator': 'EQ',
                                   'value': email.lower()}], 1)
        if rs:
            return rs[0], False
    # 2. Exact first+last name.
    filters = []
    if first:
        filters.append({'propertyName': 'firstname', 'operator': 'EQ', 'value': first})
    if last:
        filters.append({'propertyName': 'lastname', 'operator': 'EQ', 'value': last})
    if not filters:
        return None, False
    rs = _search(hs_headers, filters, 10)
    if not rs:
        return None, False
    if len(rs) == 1:
        return rs[0], False
    # 3. Several people share this name -> disambiguate by company token overlap.
    ct = company_tokens(company_name)
    if ct:
        matches = [c for c in rs
                   if ct & company_tokens((c.get('properties') or {}).get('company'))]
        if len(matches) == 1:
            return matches[0], False
    # 4. Still ambiguous -> refuse to guess, and tell the caller not to create.
    return None, True


# --- company matching (MeetingBot never creates companies) ------------------

def company_key(name):
    """Name for duplicate matching: lowercase, '&' as 'and', punctuation
    dropped, trailing legal suffixes stripped. 'Acme Insurance, Inc.' and
    'acme insurance' are one company."""
    s = (name or '').lower().replace('&', ' and ').replace('.', '')
    words = re.sub(r'[^a-z0-9]+', ' ', s).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return ' '.join(words)


def _root_domain(domain):
    d = (domain or '').strip().lower()
    for pre in ('https://', 'http://'):
        if d.startswith(pre):
            d = d[len(pre):]
    d = d.split('/')[0]
    return d[4:] if d.startswith('www.') else d


def email_domain(email):
    """Corporate domain of an email, or None for free-mail / malformed."""
    email = (email or '').strip().lower()
    if '@' not in email:
        return None
    d = _root_domain(email.split('@', 1)[1])
    return d if d and d not in FREE_EMAIL_DOMAINS else None


def _company_by_domain(hs_headers, domain, props):
    """Company whose domain root-matches `domain` (subdomains count)."""
    for body in (
        {'filterGroups': [{'filters': [{'propertyName': 'domain', 'operator': 'EQ', 'value': domain}]}]},
        {'query': domain},
    ):
        rs = hs_search(hs_headers, COMPANY_SEARCH_URL,
                       {**body, 'properties': props, 'limit': 20})
        for c in rs:
            d = _root_domain((c.get('properties') or {}).get('domain'))
            if d and (d == domain or d.endswith('.' + domain) or domain.endswith('.' + d)):
                return c
    return None


def _company_by_name(hs_headers, name, props):
    """Same company_key (case, punctuation, legal suffix ignored). HubSpot's
    EQ filter is case-sensitive and exact, so full-text search on the key
    and compare client-side."""
    key = company_key(name)
    if not key:
        return None
    query = ' '.join(w for w in key.split() if w != 'and') or key
    rs = hs_search(hs_headers, COMPANY_SEARCH_URL,
                   {'query': query, 'properties': props, 'limit': 20})
    for c in rs:
        if company_key((c.get('properties') or {}).get('name')) == key:
            return c
    return None


def find_company(hs_headers, name, email=None):
    """Existing HubSpot company for a booking: email domain first (skips
    free-mail), then normalized name. Returns the company dict or None.
    Raises if HubSpot search fails."""
    props = ['name', 'domain', 'hubspot_owner_id']
    domain = email_domain(email)
    if domain:
        c = _company_by_domain(hs_headers, domain, props)
        if c:
            return c
    if name:
        return _company_by_name(hs_headers, name, props)
    return None
