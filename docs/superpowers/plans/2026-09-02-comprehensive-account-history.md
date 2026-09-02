# Comprehensive Account History Brief — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the returning-account booking comment from a count tally to a Claude-written narrative summary plus who-we-talked-to, sourced from prior HubSpot meetings/notes/emails.

**Architecture:** Extend the live `hs_company_history(company_id)` helper (in `meeting_bot.py`) with bounded content reads (meetings/notes/emails), participant resolution via association batch reads, a haiku summary call, and a locally-computed last-touch. Add the results to the returned dict; `_log_comment` renders a narrative+facts block when a summary exists and otherwise falls back to today's bullet block.

**Tech Stack:** Python, `requests` against HubSpot CRM v3/v4, the module-level `anthropic.Anthropic` client (`claude-haiku-4-5-20251001`), pytest with `requests`/`client` mocking.

## Global Constraints

- HubSpot auth: reuse the module-level `HS` header dict (`meeting_bot.py:48`). Never build a new auth header.
- HubSpot reads use `requests` with `timeout=30` (search / batch) / `timeout=15` (single GET).
- Every new read, batch read, and the Claude call is individually wrapped: any one failing degrades to omitting only its piece and NEVER raises out of `hs_company_history`.
- Claude: reuse the module-level `client` (`meeting_bot.py:109`), model `claude-haiku-4-5-20251001`, `max_tokens=180`. If `client` is `None` (no API key), summary is `None`. No live Claude calls in tests — mock `client`.
- The brief is strictly additive: `summary` present → comprehensive block; `summary` None but counts/deal/owner exist → today's bullet block unchanged; nothing → silent.
- Net-new companies (`co is None`) are unaffected — `hs_company_history` is never called for them.
- Bounds: meetings `limit 8`, notes `limit 10`, emails `limit 10`, participants capped at 6, per-body clip ~500 chars, assembled summary input hard-capped at 6000 chars.
- HubSpot v3 returns datetime properties (`hs_meeting_start_time`, `hs_timestamp`, `hs_last_activity_date`) as ISO-8601 strings — string comparison and `_day()` slicing are valid across all three.
- No new dependencies. Commit style: subject line only, no body, NO Co-Authored-By trailer.

---

### Task 1: Content-gathering sub-reads

**Files:**
- Modify: `meeting_bot.py` — add `_search_objects` and `_gather_account_content` after `_hs_search_total` (currently near line 802), before `_day`.
- Test: `tests/test_account_content.py` (create)

**Interfaces:**
- Consumes: module globals `HS`.
- Produces:
  - `_search_objects(obj, company_id, props, sort_prop, limit) -> list[dict]` — each dict is the object's `properties` merged with its `id`. `[]` on failure.
  - `_gather_account_content(company_id) -> {'meetings': list, 'notes': list, 'emails': list}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_account_content.py
import meeting_bot


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_search_objects_merges_id(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert 'meetings/search' in url
        assert json['sorts'][0]['propertyName'] == 'hs_meeting_start_time'
        assert json['limit'] == 8
        return _Resp(200, {'results': [
            {'id': 'M1', 'properties': {'hs_meeting_title': 'Intro', 'hs_meeting_start_time': '2026-06-14T10:00:00Z'}},
        ]})
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    out = meeting_bot._search_objects('meetings', 'C1',
        ['hs_meeting_title', 'hs_meeting_start_time'], 'hs_meeting_start_time', 8)
    assert out == [{'id': 'M1', 'hs_meeting_title': 'Intro', 'hs_meeting_start_time': '2026-06-14T10:00:00Z'}]


def test_search_objects_degrades(monkeypatch):
    monkeypatch.setattr(meeting_bot.requests, 'post', lambda *a, **k: _Resp(500, {}))
    assert meeting_bot._search_objects('notes', 'C1', ['hs_note_body'], 'hs_timestamp', 10) == []


def test_gather_account_content_three_sources(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'meetings/search' in url:
            return _Resp(200, {'results': [{'id': 'M1', 'properties': {'hs_meeting_title': 'Intro'}}]})
        if 'notes/search' in url:
            return _Resp(200, {'results': [{'id': 'N1', 'properties': {'hs_note_body': 'called'}}]})
        if 'emails/search' in url:
            return _Resp(200, {'results': [{'id': 'E1', 'properties': {'hs_email_subject': 'Re: pricing'}}]})
        raise AssertionError(url)
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    c = meeting_bot._gather_account_content('C1')
    assert c['meetings'][0]['id'] == 'M1'
    assert c['notes'][0]['hs_note_body'] == 'called'
    assert c['emails'][0]['hs_email_subject'] == 'Re: pricing'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_account_content.py -v`
Expected: FAIL with `AttributeError: module 'meeting_bot' has no attribute '_search_objects'`

- [ ] **Step 3: Implement**

Insert after `_hs_search_total` (near line 802):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_account_content.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_account_content.py
git commit -m "feat: gather prior meetings/notes/emails for account brief"
```

---

### Task 2: Participant resolution

**Files:**
- Modify: `meeting_bot.py` — add `_assoc_contact_ids`, `_contacts_display`, `_account_participants` after `_gather_account_content`.
- Test: `tests/test_account_content.py` (append)

**Interfaces:**
- Consumes: `_gather_account_content`'s meeting/email lists (dicts carrying `id`).
- Produces:
  - `_assoc_contact_ids(obj, obj_ids) -> set[str]` (v4 batch read; `set()` on failure).
  - `_contacts_display(contact_ids) -> list[{'name': str, 'title': str|None}]` (v3 batch read, capped 6).
  - `_account_participants(meetings, emails) -> list[{'name','title'}]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_account_content.py
def test_account_participants_resolves_names(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'associations/meetings/contacts/batch/read' in url:
            return _Resp(200, {'results': [{'from': {'id': 'M1'}, 'to': [{'toObjectId': 101}]}]})
        if 'associations/emails/contacts/batch/read' in url:
            return _Resp(200, {'results': [{'from': {'id': 'E1'}, 'to': [{'toObjectId': 102}]}]})
        if 'contacts/batch/read' in url:
            ids = {i['id'] for i in json['inputs']}
            assert ids == {'101', '102'}
            return _Resp(200, {'results': [
                {'id': '101', 'properties': {'firstname': 'Jane', 'lastname': 'Doe', 'jobtitle': 'VP Ops'}},
                {'id': '102', 'properties': {'firstname': 'Mark', 'lastname': 'Lee', 'jobtitle': None}},
            ]})
        raise AssertionError(url)
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    out = meeting_bot._account_participants([{'id': 'M1'}], [{'id': 'E1'}])
    names = {p['name']: p['title'] for p in out}
    assert names == {'Jane Doe': 'VP Ops', 'Mark Lee': None}


def test_account_participants_degrades(monkeypatch):
    monkeypatch.setattr(meeting_bot.requests, 'post', lambda *a, **k: _Resp(500, {}))
    assert meeting_bot._account_participants([{'id': 'M1'}], []) == []


def test_contacts_display_caps_at_six(monkeypatch):
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured['n'] = len(json['inputs'])
        return _Resp(200, {'results': []})
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    meeting_bot._contacts_display([str(i) for i in range(20)])
    assert captured['n'] == 6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_account_content.py -k participants -v`
Expected: FAIL — `module 'meeting_bot' has no attribute '_account_participants'`

- [ ] **Step 3: Implement**

Insert after `_gather_account_content`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_account_content.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_account_content.py
git commit -m "feat: resolve meeting/email participants for account brief"
```

---

### Task 3: Claude summary + last-touch

**Files:**
- Modify: `meeting_bot.py` — add `_clip`, `_summarize_account`, `_last_touch` after `_account_participants`.
- Test: `tests/test_account_content.py` (append)

**Interfaces:**
- Consumes: content lists from Task 1; module `client`, `_day` (existing).
- Produces:
  - `_summarize_account(meetings, notes, emails) -> str | None`.
  - `_last_touch(content, fallback_date) -> {'type': str, 'date': str} | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_account_content.py
class _Block:
    def __init__(self, text):
        self.type = 'text'
        self.text = text

class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


def test_summarize_account_calls_claude(monkeypatch):
    captured = {}
    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return _Msg('Two calls since March on claims automation.')
    monkeypatch.setattr(meeting_bot, 'client', type('C', (), {'messages': FakeMessages()})())
    out = meeting_bot._summarize_account(
        [{'hs_meeting_title': 'Intro', 'hs_meeting_body': 'discussed claims'}],
        [{'hs_note_body': 'left vm'}],
        [{'hs_email_subject': 'pricing', 'hs_email_text': 'sent quote'}])
    assert out == 'Two calls since March on claims automation.'
    assert captured['model'] == 'claude-haiku-4-5-20251001'
    assert captured['max_tokens'] == 180
    assert len(captured['messages'][0]['content']) <= 6000


def test_summarize_account_empty_returns_none(monkeypatch):
    monkeypatch.setattr(meeting_bot, 'client', object())  # never called
    assert meeting_bot._summarize_account([], [], []) is None


def test_summarize_account_claude_error_returns_none(monkeypatch):
    class FakeMessages:
        def create(self, **kw):
            raise RuntimeError('boom')
    monkeypatch.setattr(meeting_bot, 'client', type('C', (), {'messages': FakeMessages()})())
    assert meeting_bot._summarize_account([{'hs_meeting_title': 'Intro'}], [], []) is None


def test_last_touch_picks_most_recent(monkeypatch):
    content = {
        'meetings': [{'hs_meeting_start_time': '2026-06-14T10:00:00Z'}],
        'notes': [{'hs_timestamp': '2026-05-01T00:00:00Z'}],
        'emails': [{'hs_timestamp': '2026-08-20T00:00:00Z'}],
    }
    assert meeting_bot._last_touch(content, None) == {'type': 'email', 'date': '2026-08-20'}


def test_last_touch_falls_back_to_activity(monkeypatch):
    content = {'meetings': [], 'notes': [], 'emails': []}
    assert meeting_bot._last_touch(content, '2026-07-01T00:00:00Z') == {'type': 'activity', 'date': '2026-07-01'}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_account_content.py -k "summarize or last_touch" -v`
Expected: FAIL — `module 'meeting_bot' has no attribute '_summarize_account'`

- [ ] **Step 3: Implement**

Insert after `_account_participants`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_account_content.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_account_content.py
git commit -m "feat: Claude account summary and last-touch computation"
```

---

### Task 4: Wire comprehensive fields into `hs_company_history`

**Files:**
- Modify: `meeting_bot.py` — inside `hs_company_history`, after the existing owner/last-activity block, before `return h or None`.
- Test: `tests/test_company_history.py` (append)

**Interfaces:**
- Consumes: `_gather_account_content`, `_account_participants`, `_summarize_account`, `_last_touch` (Tasks 1-3).
- Produces: `hs_company_history` return dict gains `summary: str|None`, `participants: list`, `last_touch: dict|None` (keys set only when truthy).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_company_history.py
def test_history_includes_summary_and_participants(monkeypatch):
    # search endpoints: meetings/deals/contacts/notes/emails; batch reads; company GET
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'meetings/search' in url:
            return _Resp(200, {'total': 2, 'results': [{'id': 'M1', 'properties':
                {'hs_meeting_title': 'Intro', 'hs_meeting_start_time': '2026-06-14T10:00:00Z',
                 'hs_meeting_body': 'claims automation'}}]})
        if 'deals/search' in url:
            return _Resp(200, {'total': 0, 'results': []})
        if 'contacts/search' in url:
            return _Resp(200, {'total': 5, 'results': []})
        if 'notes/search' in url:
            return _Resp(200, {'results': []})
        if 'emails/search' in url:
            return _Resp(200, {'results': []})
        if 'associations/meetings/contacts/batch/read' in url:
            return _Resp(200, {'results': [{'from': {'id': 'M1'}, 'to': [{'toObjectId': 101}]}]})
        if 'associations/emails/contacts/batch/read' in url:
            return _Resp(200, {'results': []})
        if 'contacts/batch/read' in url:
            return _Resp(200, {'results': [{'id': '101', 'properties':
                {'firstname': 'Jane', 'lastname': 'Doe', 'jobtitle': 'VP Ops'}}]})
        raise AssertionError(url)
    def fake_get(url, headers=None, params=None, timeout=None):
        return _Resp(200, {'properties': {'hubspot_owner_id': '162210484',
                                          'hs_last_activity_date': '2026-05-01T00:00:00Z'}})
    class FakeMessages:
        def create(self, **kw):
            return type('M', (), {'content': [type('B', (), {'type': 'text',
                'text': 'One intro call on claims automation.'})()]})()
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    monkeypatch.setattr(meeting_bot.requests, 'get', fake_get)
    monkeypatch.setattr(meeting_bot, 'client', type('C', (), {'messages': FakeMessages()})())

    h = meeting_bot.hs_company_history('C1')
    assert h['summary'] == 'One intro call on claims automation.'
    assert h['participants'] == [{'name': 'Jane Doe', 'title': 'VP Ops'}]
    assert h['last_touch'] == {'type': 'meeting', 'date': '2026-06-14'}
    # existing keys still present
    assert h['meetings_count'] == 2
    assert h['contacts_count'] == 5
```

Note: the `_Resp` helper already exists in `tests/test_company_history.py` from the prior feature — reuse it, do not redefine.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_company_history.py -k summary_and_participants -v`
Expected: FAIL — `KeyError: 'summary'`

- [ ] **Step 3: Implement**

In `hs_company_history`, immediately before `return h or None`, insert:

```python
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
```

Note: `h.get('last_activity_date')` was already set (as `YYYY-MM-DD`) by the owner block above; `_day()` is idempotent on an already-sliced date, so passing it as the fallback is safe.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_company_history.py -v`
Expected: PASS (all, including the new test)

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_company_history.py
git commit -m "feat: attach summary, participants, last-touch to company history"
```

---

### Task 5: Render the comprehensive block in `_log_comment`

**Files:**
- Modify: `meeting_bot.py:1158-1177` (the `if history:` block inside `_log_comment`)
- Test: `tests/test_company_history.py` (append)

**Interfaces:**
- Consumes: `hs_company_history` dict with `summary`/`participants`/`last_touch` (Task 4) plus existing keys.
- Produces: no new interface — rendering change only.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_company_history.py
def test_render_comprehensive_block():
    parsed = {'company_name': 'Acme', 'segment': 'brokerage', 'company_size': 2000}
    history = {
        'summary': 'Two calls since March on claims automation; stalled on pricing.',
        'participants': [{'name': 'Jane Doe', 'title': 'VP Ops'}, {'name': 'Mark Lee', 'title': None}],
        'deal': {'name': 'Acme - Intro Calls', 'stage': 'appointmentscheduled', 'amount': '40000', 'open': True},
        'last_touch': {'type': 'email', 'date': '2026-08-20'},
        'meetings_count': 3, 'contacts_count': 5, 'owner_name': 'jacob',
    }
    out = meeting_bot._log_comment(parsed, False, poster='U1', history=history)
    assert '📋 *Acme* — account history' in out
    assert 'Two calls since March' in out
    assert '• Talked to: Jane Doe (VP Ops), Mark Lee' in out
    assert '• Open deal: Acme - Intro Calls (appointmentscheduled, $40000)' in out
    assert '• Last touch: email, 2026-08-20' in out
    # comprehensive block replaces the bullet tally
    assert "We've spoken to" not in out


def test_render_falls_back_to_bullets_without_summary():
    parsed = {'company_name': 'Acme'}
    history = {'meetings_count': 3, 'last_meeting_date': '2026-06-14', 'contacts_count': 5}
    out = meeting_bot._log_comment(parsed, False, poster='U1', history=history)
    assert "📋 We've spoken to *Acme* before:" in out
    assert '3 prior meetings (last: 2026-06-14)' in out
    assert 'account history' not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_company_history.py -k "comprehensive or falls_back" -v`
Expected: FAIL — the comprehensive header assertion fails (current code only renders the bullet block)

- [ ] **Step 3: Implement**

Replace the entire current block (lines 1158-1177, from `if history:` down to and including `lines += hist`) with:

```python
    if history and history.get('summary'):
        lines.append(f"📋 *{company}* — account history")
        lines.append(history['summary'])
        parts = history.get('participants') or []
        if parts:
            who = ', '.join(p['name'] + (f" ({p['title']})" if p.get('title') else '') for p in parts)
            lines.append(f"• Talked to: {who}")
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
```

Note: the `elif` branch is today's block verbatim, with one fix carried in — `deal.get('stage') or ''` (was `deal.get('stage')`, which rendered literal `None` when a stage was absent).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_company_history.py -v`
Expected: PASS (all)

- [ ] **Step 5: Run the full suite and import check**

Run: `python3 -m pytest -q` → all pass.
Run: `python3 -c "import meeting_bot; print('ok')"` → `ok`.

- [ ] **Step 6: Commit**

```bash
git add meeting_bot.py tests/test_company_history.py
git commit -m "feat: render comprehensive account brief with fallback"
```

---

## Self-Review

- **Spec coverage:** Content gathering (meetings/notes/emails) → Task 1. Participants via association + contact batch reads → Task 2. Claude summary + local last-touch → Task 3. Return-shape additions → Task 4. Narrative+facts rendering with the summary→bullets→silent degradation ladder → Task 5. Net-new-silent is unchanged (helper not called; guarded by existing tests). All spec sections mapped.
- **Placeholder scan:** No TBD/TODO; every code step shows complete code.
- **Type consistency:** `_search_objects`→`_gather_account_content`→`_account_participants`/`_summarize_account`/`_last_touch`→`hs_company_history` dict keys (`summary`, `participants` as `[{'name','title'}]`, `last_touch` as `{'type','date'}`) are identical across producer (Task 4), renderer (Task 5), and tests. Bounds (8/10/10/6/500/6000) and model/`max_tokens` match the Global Constraints and spec verbatim.
