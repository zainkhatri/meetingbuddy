# HubSpot Account History in Booking Comment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a BDR books a meeting for a company already in HubSpot, append a prior-history brief (meetings, deals, contacts, owner/last-activity) to the bot's in-thread comment; net-new companies are unchanged.

**Architecture:** Snapshot history in `_process_booking` at the point the company is looked up (line 1103, before any writes) so counts never include the just-booked record and net-new companies (`co is None`) skip all extra API calls. A new `hs_company_history()` helper does the reads; `_log_comment` gains a `history` param that renders the block.

**Tech Stack:** Python, `requests` against HubSpot CRM v3, existing pytest suite with `requests` mocking.

## Global Constraints

- HubSpot auth: reuse the module-level `HS` header dict (`meeting_bot.py:48`). Never build a new auth header.
- All HubSpot reads use `requests` with `timeout=30` (search) / `timeout=15` (single GET), mirroring existing helpers.
- Each history sub-read degrades independently: a non-200 or exception omits that line only and never raises out of `hs_company_history`.
- Net-new companies (`co is None`) produce byte-for-byte the same comment as today — no history block, no extra API calls.
- No new dependencies, no config toggle, no caching beyond the owner-id→name dict.
- Commit style: subject line only, no body, no Co-Authored-By trailer.

---

### Task 1: `hs_company_history` helper

**Files:**
- Modify: `meeting_bot.py` — add function after `ensure_deal` (near line 732), before `_SEGMENT_LABELS` (line 996). Add an `_OWNER_NAME_CACHE` dict + inverted-roster lookup near the new function.
- Test: `tests/test_company_history.py` (create)

**Interfaces:**
- Consumes: module globals `HS` (line 48), `DEAL_OPEN_STAGES` (line 660), `NAME_TO_OWNER` (line 80).
- Produces: `hs_company_history(company_id, contact_id) -> dict | None`. Return dict keys (all optional): `meetings_count: int`, `last_meeting_date: str`, `deal: {'name': str, 'stage': str, 'amount': str|None, 'open': bool}`, `contacts_count: int`, `owner_name: str`, `last_activity_date: str`. Returns `None` when every field is empty/failed.
- Produces: `_owner_name(owner_id) -> str | None` (owner id → display name, roster-first then cached HubSpot lookup).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_company_history.py
import types
import meeting_bot


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _install(monkeypatch, *, meetings=None, deals=None, contacts=None, company=None, owner=None):
    """Route HubSpot reads by URL. Missing kwargs -> 500 (simulates a failing sub-read)."""
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'meetings/search' in url:
            return _Resp(200, meetings) if meetings is not None else _Resp(500, {})
        if 'deals/search' in url:
            return _Resp(200, deals) if deals is not None else _Resp(500, {})
        if 'contacts/search' in url:
            return _Resp(200, contacts) if contacts is not None else _Resp(500, {})
        raise AssertionError(f'unexpected POST {url}')

    def fake_get(url, headers=None, params=None, timeout=None):
        if '/objects/companies/' in url:
            return _Resp(200, company) if company is not None else _Resp(500, {})
        if '/owners/' in url:
            return _Resp(200, owner) if owner is not None else _Resp(500, {})
        raise AssertionError(f'unexpected GET {url}')

    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    monkeypatch.setattr(meeting_bot.requests, 'get', fake_get)


def test_full_history(monkeypatch):
    _install(
        monkeypatch,
        meetings={'total': 3, 'results': [{'properties': {'hs_meeting_start_time': '2026-06-14T10:00:00Z'}}]},
        deals={'total': 1, 'results': [{'properties': {'dealname': 'Acme - Intro Calls',
                                                       'dealstage': 'appointmentscheduled', 'amount': '40000'}}]},
        contacts={'total': 5, 'results': []},
        company={'properties': {'hubspot_owner_id': '162210484', 'hs_last_activity_date': '2026-08-20T00:00:00Z'}},
    )
    h = meeting_bot.hs_company_history('C1', 'K1')
    assert h['meetings_count'] == 3
    assert h['last_meeting_date'] == '2026-06-14'
    assert h['deal']['name'] == 'Acme - Intro Calls'
    assert h['deal']['open'] is True
    assert h['contacts_count'] == 5
    assert h['owner_name'] == 'jacob'          # roster lookup, no /owners/ GET needed
    assert h['last_activity_date'] == '2026-08-20'


def test_partial_degrades(monkeypatch):
    # meetings read fails (500), everything else empty -> only contacts present
    _install(monkeypatch, contacts={'total': 2, 'results': []})
    h = meeting_bot.hs_company_history('C1', 'K1')
    assert h == {'contacts_count': 2}


def test_all_empty_returns_none(monkeypatch):
    _install(monkeypatch,
             meetings={'total': 0, 'results': []},
             deals={'total': 0, 'results': []},
             contacts={'total': 0, 'results': []},
             company={'properties': {}})
    assert meeting_bot.hs_company_history('C1', 'K1') is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_company_history.py -v`
Expected: FAIL with `AttributeError: module 'meeting_bot' has no attribute 'hs_company_history'`

- [ ] **Step 3: Write the implementation**

Add near line 732 (after `ensure_deal`):

```python
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
            _OWNER_NAME_CACHE[owner_id] = name or None
            return _OWNER_NAME_CACHE[owner_id]
    except Exception:
        pass
    return None


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


def _day(ts):
    """'2026-06-14T10:00:00Z' -> '2026-06-14'. None-safe."""
    return ts[:10] if ts else None


def hs_company_history(company_id, contact_id):
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

    return h or None
```

Note: the `contacts` search passes empty `props`; `_hs_search_total` skips the `sorts` clause when `props` is empty, so HubSpot returns `total` without a sort error.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_company_history.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_company_history.py
git commit -m "feat: hs_company_history helper for prior-account brief"
```

---

### Task 2: Render history block in `_log_comment`

**Files:**
- Modify: `meeting_bot.py:1021-1049` (`_log_comment`)
- Test: `tests/test_company_history.py` (append)

**Interfaces:**
- Consumes: `hs_company_history` return dict from Task 1.
- Produces: `_log_comment(parsed, is_conference, poster=None, history=None) -> str`. When `history` is falsy the output is unchanged from today.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_company_history.py
def test_comment_renders_history_block():
    parsed = {'company_name': 'Acme', 'segment': 'brokerage', 'company_size': 2000}
    history = {'meetings_count': 3, 'last_meeting_date': '2026-06-14',
               'deal': {'name': 'Acme - Intro Calls', 'stage': 'appointmentscheduled',
                        'amount': '40000', 'open': True},
               'contacts_count': 5, 'owner_name': 'jacob', 'last_activity_date': '2026-08-20'}
    out = meeting_bot._log_comment(parsed, False, poster='U1', history=history)
    assert 'spoken to' in out
    assert '3 prior meetings (last: 2026-06-14)' in out
    assert 'Acme - Intro Calls' in out
    assert '5 contacts on file' in out
    assert 'jacob' in out


def test_comment_unchanged_without_history():
    parsed = {'company_name': 'Acme', 'segment': 'brokerage', 'company_size': 2000}
    base = meeting_bot._log_comment(parsed, False, poster='U1')
    with_none = meeting_bot._log_comment(parsed, False, poster='U1', history=None)
    assert base == with_none
    assert 'spoken to' not in base
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_company_history.py -k comment -v`
Expected: FAIL — `_log_comment() got an unexpected keyword argument 'history'`

- [ ] **Step 3: Implement**

Change the signature at `meeting_bot.py:1021`:

```python
def _log_comment(parsed, is_conference, poster=None, history=None):
```

Then, immediately before the final `lines.append(f"Nice meeting, {who}! 🎉")` (line 1048), insert:

```python
    if history:
        hist = []
        if history.get('meetings_count'):
            last = history.get('last_meeting_date')
            hist.append(f"• {history['meetings_count']} prior meetings"
                        + (f" (last: {last})" if last else ""))
        deal = history.get('deal')
        if deal and deal.get('name'):
            amt = f", ${deal['amount']}" if deal.get('amount') else ""
            label = 'Open deal' if deal.get('open') else 'Deal'
            hist.append(f"• {label}: {deal['name']} ({deal.get('stage')}{amt})")
        if history.get('contacts_count'):
            hist.append(f"• {history['contacts_count']} contacts on file")
        if history.get('owner_name'):
            act = history.get('last_activity_date')
            hist.append(f"• Owner: {history['owner_name']}"
                        + (f" · last activity {act}" if act else ""))
        if hist:
            lines.append(f"📋 We've spoken to *{company}* before:")
            lines += hist
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_company_history.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_company_history.py
git commit -m "feat: render prior-account history in booking comment"
```

---

### Task 3: Wire the snapshot into `_process_booking`

**Files:**
- Modify: `meeting_bot.py:1104` (capture), `meeting_bot.py:1197` and `meeting_bot.py:1304` (pass to `_log_comment`)

**Interfaces:**
- Consumes: `hs_company_history` (Task 1), `_log_comment(..., history=...)` (Task 2), local `co`/`company_id`/`contact_id` already in scope in `_process_booking`.
- Produces: no new interface — behavior change only.

- [ ] **Step 1: Capture the snapshot before writes**

After `meeting_bot.py:1104` (`company_id = co['id'] if co else None`), add:

```python
    # Snapshot prior HubSpot footprint BEFORE any writes, so counts exclude the
    # meeting/deal we're about to create. Net-new company (co is None) -> no reads.
    history = hs_company_history(company_id, contact_id) if co else None
```

Note: `contact_id` is assigned at line 1111, after this point. Move the `history = ...` line to immediately after `contact_id = contact['id'] if contact else None` (line 1111) so `contact_id` is defined — still before any meeting/deal write.

- [ ] **Step 2: Pass history at both call sites**

At `meeting_bot.py:1197`:

```python
        _note = _log_comment(parsed, bool(profile.get('is_conference')), poster, history=history)
```

At `meeting_bot.py:1304`:

```python
        note = _log_comment(parsed, bool(profile.get('is_conference')), poster, history=history)
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (existing suite + 5 new tests, no regressions)

- [ ] **Step 4: Smoke-check import**

Run: `python -c "import meeting_bot; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py
git commit -m "feat: attach account-history snapshot to booking comments"
```

---

## Self-Review

- **Spec coverage:** Four history items (meetings, deals, contacts, owner/last-activity) → Task 1. Net-new stays silent (`co is None` gate) → Task 3 Step 1. Snapshot-before-write correctness → Task 3 Step 1. Rendering → Task 2. Independent degradation → Task 1 `_hs_search_total`/try-except + `test_partial_degrades`. All spec sections mapped.
- **Placeholder scan:** No TBD/TODO; every code step shows full code.
- **Type consistency:** `hs_company_history` return keys (`meetings_count`, `last_meeting_date`, `deal{name,stage,amount,open}`, `contacts_count`, `owner_name`, `last_activity_date`) are identical across Task 1 producer, Task 2 renderer, and the tests. `history` kwarg name consistent across Tasks 2 and 3.
