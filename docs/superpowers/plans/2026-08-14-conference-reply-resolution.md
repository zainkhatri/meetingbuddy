# Conference Reply Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `#conference-meetings` booking has no identifiable conference, the bot asks in-thread which conference it is; a human's thread reply is caught, resolved, and re-stamped onto the meeting's `conference_source` in HubSpot.

**Architecture:** Stateless. A thread reply's `thread_ts` equals the parent booking's `ts`, and every meeting is stamped with `booked_at` = the Slack post ts (ms). So the reply handler finds the meeting by a `booked_at EQ` search — no stored `thread_ts→meeting_id` map, survives the 30-min periodic restart, no re-parse of the parent through Claude.

**Tech Stack:** Python 3, Slack Bolt (`@app.event('message')`), `requests` (HubSpot REST), pytest with monkeypatch.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-14-conference-reply-resolution-design.md`.
- HubSpot meetings property: `conference_source` on object type `meetings`. Auth via module-level `HS` headers dict (`meeting_bot.py:47`). Meetings search: `https://api.hubapi.com/crm/v3/objects/meetings/search`. Single-meeting PATCH: `https://api.hubapi.com/crm/v3/objects/meetings/{id}`.
- The reply handler only ever overrides `conference_source` when it is `None` or `'other'` — never an already-resolved value.
- All HubSpot calls wrapped in try/except with `print(..., flush=True)` on failure (module's existing style); nothing raises out of the handler.
- Existing helpers reused as-is: `detect_conference_from_title(title) -> str|None`, `resolve_or_create_conference(raw, meeting_date) -> {'value','created','label'}|None`, `hs_conference_options(force=False) -> list[{'value','label','hidden'}]`. Module constant `CONFERENCE_MEETINGS_CHANNEL` (`meeting_bot.py:68`).
- Commit messages: subject line only, no body, no Co-Authored-By (per user global rules).
- Tests live in `tests/test_conference_reply.py` (new file); bootstrap identical to `tests/test_conference_autocreate.py` (imports `meeting_bot` offline via `tests/conftest.py`).

---

### Task 1: `_find_meeting_by_booked_at` — link a thread to its meeting

**Files:**
- Modify: `meeting_bot.py` (add near `_maybe_unsure_reply`, ~line 922)
- Test: `tests/test_conference_reply.py` (create)

**Interfaces:**
- Consumes: module `HS`, `requests`, `datetime` (already imported).
- Produces: `_find_meeting_by_booked_at(thread_ts) -> dict | None` → `{'id': str, 'conference_source': str|None, 'meeting_date': str|None}` (meeting_date is `'YYYY-MM-DD'` derived from `hs_meeting_start_time`, or `None`). Returns `None` when no meeting carries that `booked_at` or on any error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conference_reply.py
import meeting_bot as mb


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


def test_find_meeting_by_booked_at_hit(monkeypatch):
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured['json'] = json
        return _Resp(200, {'total': 1, 'results': [{
            'id': '42',
            'properties': {'conference_source': 'other',
                           'hs_meeting_start_time': '1786662392000'}}]})
    monkeypatch.setattr(mb.requests, 'post', fake_post)
    out = mb._find_meeting_by_booked_at('1786662392.092149')
    assert out == {'id': '42', 'conference_source': 'other', 'meeting_date': '2026-08-14'}
    # searched by booked_at in ms
    f = captured['json']['filterGroups'][0]['filters'][0]
    assert f == {'propertyName': 'booked_at', 'operator': 'EQ', 'value': '1786662392092'}


def test_find_meeting_by_booked_at_miss(monkeypatch):
    monkeypatch.setattr(mb.requests, 'post',
                        lambda *a, **k: _Resp(200, {'total': 0, 'results': []}))
    assert mb._find_meeting_by_booked_at('1786662392.092149') is None


def test_find_meeting_by_booked_at_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('network')
    monkeypatch.setattr(mb.requests, 'post', boom)
    assert mb._find_meeting_by_booked_at('1786662392.092149') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/nvme/PROMETHEUS/WORK/FAI/meetings/meetingbuddy && python3 -m pytest tests/test_conference_reply.py -k find_meeting -q`
Expected: FAIL — `AttributeError: module 'meeting_bot' has no attribute '_find_meeting_by_booked_at'`

- [ ] **Step 3: Write minimal implementation**

```python
# meeting_bot.py — add just above _maybe_unsure_reply (~line 922)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_conference_reply.py -k find_meeting -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_reply.py
git commit -m "feat(conf-reply): find meeting from thread via booked_at"
```

---

### Task 2: `_handle_conference_reply` — resolve the answer, re-stamp

**Files:**
- Modify: `meeting_bot.py` (add after `_find_meeting_by_booked_at`)
- Test: `tests/test_conference_reply.py`

**Interfaces:**
- Consumes: `_find_meeting_by_booked_at`, `detect_conference_from_title`, `resolve_or_create_conference`, `hs_conference_options`, `HS`, `requests`.
- Produces: `_handle_conference_reply(thread_ts, text, say) -> None`. Side effects only: PATCHes `conference_source` on the found meeting and replies in-thread; silent no-op when there is no meeting or it already has a real conference.
- Helper: `_conf_label(value) -> str` — human label for a conference value via `hs_conference_options`, falling back to `value`.

- [ ] **Step 1: Write the failing test**

```python
def _stub_meeting(monkeypatch, meeting):
    monkeypatch.setattr(mb, '_find_meeting_by_booked_at', lambda ts: meeting)

def _capture_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(mb.requests, 'patch',
                        lambda url, headers=None, json=None, timeout=None:
                            calls.append((url, json)) or _Resp(200, {}))
    return calls

def _capture_say():
    said = []
    def say(text=None, thread_ts=None):
        said.append(text)
    return say, said


def test_reply_resolves_existing_conf(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '42', 'conference_source': 'other', 'meeting_date': '2026-08-14'})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: 'wsia_uw_summit')
    monkeypatch.setattr(mb, 'hs_conference_options',
                        lambda force=False: [{'value': 'wsia_uw_summit', 'label': 'WSIA UW Summit 2026'}])
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls[0][0].endswith('/meetings/42')
    assert calls[0][1] == {'properties': {'conference_source': 'wsia_uw_summit'}}
    assert '✓' in said[0] and 'WSIA UW Summit 2026' in said[0]


def test_reply_creates_new_conf(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '7', 'conference_source': None, 'meeting_date': '2026-09-01'})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: None)
    monkeypatch.setattr(mb, 'resolve_or_create_conference',
                        lambda raw, date: {'value': 'broker_tech_conference_2026',
                                           'created': True, 'label': 'Broker Tech Conference 2026'})
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'Broker Tech Conference', say)
    assert calls[0][1] == {'properties': {'conference_source': 'broker_tech_conference_2026'}}
    assert 'Broker Tech Conference 2026' in said[0]


def test_reply_noop_when_no_meeting(monkeypatch):
    _stub_meeting(monkeypatch, None)
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls == [] and said == []


def test_reply_noop_when_already_tagged(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '9', 'conference_source': 'tmpaa', 'meeting_date': '2026-08-14'})
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls == [] and said == []


def test_reply_unresolved(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '3', 'conference_source': 'other', 'meeting_date': None})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: None)
    monkeypatch.setattr(mb, 'resolve_or_create_conference', lambda raw, date: None)
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'asdf', say)
    assert calls == []
    assert 'Still couldn\'t identify' in said[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_conference_reply.py -k reply -q`
Expected: FAIL — no attribute `_handle_conference_reply`

- [ ] **Step 3: Write minimal implementation**

```python
# meeting_bot.py — after _find_meeting_by_booked_at
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_conference_reply.py -k reply -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_reply.py
git commit -m "feat(conf-reply): resolve thread answer and re-stamp conference_source"
```

---

### Task 3: Wire routing + reword the question

**Files:**
- Modify: `meeting_bot.py` — `handle_message` (~line 840, right after `text`/`ts` are set) and `_maybe_unsure_reply` (`meeting_bot.py:928`)
- Test: `tests/test_conference_reply.py`

**Interfaces:**
- Consumes: `_handle_conference_reply`, `CONFERENCE_MEETINGS_CHANNEL`.
- Produces: routing behavior — a conference-channel thread reply dispatches to `_handle_conference_reply` and returns before the booking-parse path.

- [ ] **Step 1: Reword the unsure question**

In `_maybe_unsure_reply` (`meeting_bot.py:928`), change:

```python
        say(text="Not sure what conference that is", thread_ts=ts)
```

to:

```python
        say(text="Not sure what conference that is — reply with the name and I'll tag it.", thread_ts=ts)
```

- [ ] **Step 2: Add the routing test**

The routing lives inside the Bolt `handle_message` closure, so test the decision predicate directly by extracting it into a tiny pure helper `_is_conference_reply(event, ts)` and asserting it. Add to `tests/test_conference_reply.py`:

```python
def test_is_conference_reply_true():
    ev = {'thread_ts': '111.1', 'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is True

def test_is_conference_reply_false_toplevel():
    # top-level booking post: thread_ts absent
    ev = {'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is False

def test_is_conference_reply_false_parent_equals_ts():
    # Slack sets thread_ts == ts on a thread PARENT; that's still a booking, not a reply
    ev = {'thread_ts': '222.2', 'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is False

def test_is_conference_reply_false_other_channel():
    ev = {'thread_ts': '111.1', 'channel': 'C_OTHER'}
    assert mb._is_conference_reply(ev, '222.2') is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_conference_reply.py -k is_conference_reply -q`
Expected: FAIL — no attribute `_is_conference_reply`

- [ ] **Step 4: Add the predicate helper**

Add just above `handle_message` (`meeting_bot.py:816`):

```python
def _is_conference_reply(event, ts):
    """True when a message is a human's thread reply in the conference channel
    (not a top-level booking post, not a thread parent)."""
    tt = event.get('thread_ts')
    return bool(tt) and tt != ts and event.get('channel') == CONFERENCE_MEETINGS_CHANNEL
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_conference_reply.py -k is_conference_reply -q`
Expected: PASS

- [ ] **Step 6: Wire it into `handle_message`**

In `handle_message`, immediately after the `if not text or not ts: return` guard (`meeting_bot.py:840-841`) and BEFORE `_claim_ts(ts)`, insert:

```python
    if _is_conference_reply(event, ts):
        _handle_conference_reply(event['thread_ts'], text, say)
        return
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (existing suite + `test_conference_reply.py`).

- [ ] **Step 8: Commit**

```bash
git add meeting_bot.py tests/test_conference_reply.py
git commit -m "feat(conf-reply): route thread replies + reword the unsure question"
```

---

### Task 4: Verify sheet propagation (manual, no code unless needed)

**Files:**
- Possibly modify: `meeting_bot.py` — `_handle_conference_reply` (only if the reconcile does not carry `conference_source`)

- [ ] **Step 1: Confirm the reconcile carries conference_source**

Read `scripts/sheet_reconcile.py` and `sheet_sync.py`: does the HubSpot→sheet reconcile read `conference_source` off the meeting and write it to Ellen's tracker? If yes, a re-stamped meeting propagates on the next `sheet_reconcile_loop` pass — no code change; note it in the commit from Task 3 and stop here.

- [ ] **Step 2: If it does NOT propagate, add a direct sheet push**

Only if Step 1 shows the sheet is not updated: in `_handle_conference_reply`, after a successful PATCH, fetch the meeting's associated company/contact and call the existing `_push_to_ellen_sheet(...)` with the resolved `conference_slug=value`. Mirror the argument shape used in `_process_booking` (`meeting_bot.py:~1060`). Add a test that monkeypatches `_push_to_ellen_sheet` and asserts it is called with `conference_slug=value`. Commit:

```bash
git add meeting_bot.py tests/test_conference_reply.py
git commit -m "fix(conf-reply): push re-stamped conference to Ellen's sheet"
```

---

## Self-Review

**Spec coverage:**
- Reword unsure reply → Task 3 Step 1. Route thread replies → Task 3 (predicate + wiring). Stateless `booked_at` lookup → Task 1. Guards (no meeting / already-tagged) → Task 2. Resolve via regex then resolve-or-create → Task 2. PATCH + `✓ tagged` / `Still couldn't identify` replies → Task 2. Sheet propagation verification → Task 4. All spec sections covered.

**Placeholder scan:** none — every step has concrete code/commands. Task 4 Step 2 is conditional but fully specified.

**Type consistency:** `_find_meeting_by_booked_at -> {'id','conference_source','meeting_date'}|None` (Task 1) is consumed with those exact keys in Task 2. `detect_conference_from_title -> str|None`, `resolve_or_create_conference -> {'value','created','label'}|None`, `hs_conference_options -> [{'value','label',...}]` match their definitions in `meeting_bot.py`. `_is_conference_reply(event, ts) -> bool` (Task 3) consumed in the same task's wiring. `_conf_label(value) -> str` defined and used in Task 2. `say(text=, thread_ts=)` signature matches the Bolt `say` used throughout `meeting_bot.py`.

**Ambiguity:** guard fires only on `conference_source in (None,'other')`; label prefers `resolve_or_create`'s label / options lookup, falls back to raw value; routing excludes thread parents (`thread_ts == ts`) and other channels. Explicit.
