# Zoom recordings → HubSpot deals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll completed Zoom cloud recordings account-wide, match each to a HubSpot deal, and write a Note (summary + links + full transcript) onto the deal, plus a 30-day one-time backfill.

**Architecture:** A daemon thread inside the existing `meeting_bot.py` process on Railway runs a ~30-min poll loop. Pure helpers (transcript parse, ID extraction, note-body build, deal-choice) are unit-tested; thin HTTP wrappers talk to Zoom and HubSpot; an orchestrator wires them together with injected dependencies so it is testable with fakes. Rotating Zoom refresh token + processed-UUID set persist to a JSON file on a Railway volume.

**Tech Stack:** Python 3, `requests`, `anthropic` (existing), `pytest` (existing). No new dependencies.

---

## File Structure

- Create: `zoom_auth.py` — state-file load/save (atomic) + OAuth access-token refresh with refresh-token rotation.
- Create: `zoom_sync.py` — pure helpers, thin Zoom/HubSpot HTTP wrappers, orchestrator (`process_recording`), poll loop (`zoom_sync_loop`), and a `--backfill-days` CLI entrypoint.
- Create: `authorize_zoom.py` — one-time local helper that runs the auth-code flow against `http://localhost:3000/zoom/callback` and writes the refresh token into the state file.
- Create: `tests/test_zoom_auth.py` — state store + token rotation.
- Create: `tests/test_zoom_sync.py` — pure helpers + orchestrator with fakes.
- Modify: `meeting_bot.py` — import `zoom_sync`, start `zoom_sync_loop` daemon thread in `__main__`.
- Modify: `.env.example` — add `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_STATE_PATH`.

**Conventions to follow (from existing code):**
- HubSpot: `requests.<verb>('https://api.hubapi.com/...', headers=HS, json=..., timeout=30)`.
- Associations: `PUT https://api.hubapi.com/crm/v4/objects/{from}/{id}/associations/default/{to}/{toId}`.
- Logs: `print(f'[zoom-sync] ...')`.

---

### Task 1: State store (load/save with atomic write)

**Files:**
- Create: `zoom_auth.py`
- Test: `tests/test_zoom_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_zoom_auth.py
import json
import zoom_auth


def test_load_state_missing_returns_empty(tmp_path):
    assert zoom_auth.load_state(str(tmp_path / "nope.json")) == {}


def test_save_then_load_roundtrip(tmp_path):
    p = str(tmp_path / "zoom_state.json")
    zoom_auth.save_state(p, {"refresh_token": "rt1", "processed_recording_uuids": ["a"]})
    assert zoom_auth.load_state(p) == {"refresh_token": "rt1", "processed_recording_uuids": ["a"]}


def test_save_is_atomic_no_partial_file_left(tmp_path):
    p = str(tmp_path / "zoom_state.json")
    zoom_auth.save_state(p, {"x": 1})
    # no leftover temp file in the directory
    assert [f.name for f in tmp_path.iterdir()] == ["zoom_state.json"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zoom_auth'`

- [ ] **Step 3: Write minimal implementation**

```python
# zoom_auth.py
"""Zoom OAuth token manager + durable state store.

State lives in a JSON file on a Railway persistent volume:
    {"refresh_token": "<rotating>", "processed_recording_uuids": [...]}
Zoom rotates the refresh token on every refresh, so the new one MUST be
persisted each cycle (see get_access_token).
"""
import json
import os
import tempfile


def load_state(path):
    """Return the state dict, or {} if the file does not exist."""
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)


def save_state(path, state):
    """Atomically write state to path (write temp in same dir, then rename)."""
    d = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_auth.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_auth.py tests/test_zoom_auth.py
git commit -m "feat(zoom): durable JSON state store with atomic writes"
```

---

### Task 2: Access-token refresh with refresh-token rotation

**Files:**
- Modify: `zoom_auth.py`
- Test: `tests/test_zoom_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_auth.py

def test_get_access_token_rotates_and_persists_refresh_token(tmp_path):
    p = str(tmp_path / "zoom_state.json")
    zoom_auth.save_state(p, {"refresh_token": "old_rt", "processed_recording_uuids": []})

    calls = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"access_token": "AT", "refresh_token": "new_rt"}

    def fake_post(url, **kwargs):
        calls['url'] = url
        calls['data'] = kwargs.get('data')
        calls['auth'] = kwargs.get('auth')
        return FakeResp()

    token = zoom_auth.get_access_token(p, "cid", "secret", http_post=fake_post)

    assert token == "AT"
    assert calls['url'] == "https://zoom.us/oauth/token"
    assert calls['data'] == {"grant_type": "refresh_token", "refresh_token": "old_rt"}
    assert calls['auth'] == ("cid", "secret")
    # rotated refresh token persisted
    assert zoom_auth.load_state(p)["refresh_token"] == "new_rt"


def test_get_access_token_raises_on_failure(tmp_path):
    p = str(tmp_path / "zoom_state.json")
    zoom_auth.save_state(p, {"refresh_token": "old_rt"})

    class FakeResp:
        status_code = 400
        text = "invalid_grant"
        def json(self):
            return {}

    import pytest
    with pytest.raises(RuntimeError, match="Zoom token refresh failed"):
        zoom_auth.get_access_token(p, "cid", "secret", http_post=lambda *a, **k: FakeResp())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_auth.py -v`
Expected: FAIL — `AttributeError: module 'zoom_auth' has no attribute 'get_access_token'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_auth.py
import requests


def get_access_token(state_path, client_id, client_secret, http_post=requests.post):
    """Refresh and return a Zoom access token.

    Reads refresh_token from state, exchanges it, persists the NEW (rotated)
    refresh token back to state, and returns the access token.
    Raises RuntimeError on failure (likely an expired/rotated-out token →
    re-run authorize_zoom.py).
    """
    state = load_state(state_path)
    refresh_token = state.get('refresh_token')
    if not refresh_token:
        raise RuntimeError("Zoom token refresh failed: no refresh_token in state")
    resp = http_post(
        "https://zoom.us/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Zoom token refresh failed: {resp.status_code} {getattr(resp, 'text', '')}")
    payload = resp.json()
    new_refresh = payload.get("refresh_token")
    if new_refresh:
        state["refresh_token"] = new_refresh
        save_state(state_path, state)
    return payload["access_token"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_auth.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_auth.py tests/test_zoom_auth.py
git commit -m "feat(zoom): access-token refresh with refresh-token rotation"
```

---

### Task 3: Parse Zoom VTT transcript to plain text

**Files:**
- Create: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_zoom_sync.py
import zoom_sync

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v Zain>Hey, thanks for hopping on.

2
00:00:04.500 --> 00:00:07.000
<v Prospect>Happy to. We're evaluating a few vendors.
"""


def test_parse_vtt_strips_cues_and_keeps_text():
    out = zoom_sync.parse_vtt(VTT)
    assert "WEBVTT" not in out
    assert "00:00:01" not in out
    assert "-->" not in out
    assert "Zain: Hey, thanks for hopping on." in out
    assert "Prospect: We're evaluating a few vendors." in out


def test_parse_vtt_empty_returns_empty():
    assert zoom_sync.parse_vtt("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zoom_sync'`

- [ ] **Step 3: Write minimal implementation**

```python
# zoom_sync.py
"""Zoom cloud-recording → HubSpot deal sync.

Pure helpers (parse_vtt, extract_zoom_meeting_id, build_note_body, choose_deal)
are unit-tested. Thin Zoom/HubSpot HTTP wrappers and the orchestrator
(process_recording) are below. Runs as zoom_sync_loop() inside meeting_bot.py;
also runnable as `python3 zoom_sync.py --backfill-days 30`.
"""
import re

_CUE_TIME = re.compile(r'-->')
_SPEAKER = re.compile(r'^<v\s+([^>]+)>(.*)$')


def parse_vtt(vtt_text):
    """Convert WebVTT transcript text to 'Speaker: line' plain text."""
    if not vtt_text:
        return ""
    lines = []
    for raw in vtt_text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or _CUE_TIME.search(line):
            continue
        if line.isdigit():  # cue sequence number
            continue
        m = _SPEAKER.match(line)
        if m:
            speaker, text = m.group(1).strip(), m.group(2).strip()
            lines.append(f"{speaker}: {text}" if text else speaker)
        else:
            lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): parse VTT transcript to plain text"
```

---

### Task 4: Extract Zoom meeting ID from a join URL / text

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_extract_id_from_join_url():
    assert zoom_sync.extract_zoom_meeting_id(
        "Join: https://us02web.zoom.us/j/85512345678?pwd=abc") == "85512345678"


def test_extract_id_from_meeting_id_with_spaces():
    assert zoom_sync.extract_zoom_meeting_id("Meeting ID: 855 1234 5678") == "85512345678"


def test_extract_id_none_when_absent():
    assert zoom_sync.extract_zoom_meeting_id("no zoom here") is None
    assert zoom_sync.extract_zoom_meeting_id(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'extract_zoom_meeting_id'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
_JOIN_URL = re.compile(r'zoom\.us/j/(\d{9,12})')
_MEETING_ID = re.compile(r'(\d[\d\s]{8,13}\d)')


def extract_zoom_meeting_id(text):
    """Return the numeric Zoom meeting ID found in a join URL or 'Meeting ID:'
    string, digits only, or None."""
    if not text:
        return None
    m = _JOIN_URL.search(text)
    if m:
        return m.group(1)
    m = _MEETING_ID.search(text)
    if m:
        digits = re.sub(r'\s', '', m.group(1))
        if 9 <= len(digits) <= 12:
            return digits
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): extract Zoom meeting ID from join URL/text"
```

---

### Task 5: Build the deal Note body (with transcript truncation)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_build_note_body_includes_summary_and_links():
    body = zoom_sync.build_note_body(
        summary="Discussed pricing. Next: send proposal.",
        recording_url="https://zoom.us/rec/abc",
        transcript_url="https://zoom.us/rec/abc.vtt",
        transcript_text="Zain: hi\nProspect: hi",
    )
    assert "Discussed pricing" in body
    assert "https://zoom.us/rec/abc" in body
    assert "https://zoom.us/rec/abc.vtt" in body
    assert "Zain: hi" in body


def test_build_note_body_truncates_long_transcript():
    long_text = "x" * 100000
    body = zoom_sync.build_note_body(
        summary="s", recording_url="r", transcript_url="t",
        transcript_text=long_text, max_chars=60000,
    )
    assert len(body) <= 60000
    assert "[transcript truncated" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'build_note_body'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
def build_note_body(summary, recording_url, transcript_url, transcript_text, max_chars=60000):
    """Assemble the HubSpot note body: summary, links, then full transcript.
    Truncates the transcript so total length stays under max_chars."""
    header = (
        "<b>Zoom call summary</b><br>"
        f"{summary}<br><br>"
        f"<b>Recording:</b> {recording_url}<br>"
        f"<b>Transcript:</b> {transcript_url}<br><br>"
        "<b>Full transcript</b><br>"
    )
    trunc_note = f"<br>[transcript truncated — full recording: {recording_url}]"
    budget = max_chars - len(header)
    body_text = transcript_text or ""
    if len(body_text) > budget:
        body_text = body_text[: max(0, budget - len(trunc_note))] + trunc_note
    return header + body_text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): build deal note body with transcript truncation"
```

---

### Task 6: Deal-choice decision (0 / 1 / many)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_choose_deal_single_match():
    assert zoom_sync.choose_deal(["123"]) == ("123", "matched")


def test_choose_deal_no_match():
    assert zoom_sync.choose_deal([]) == (None, "no_deal")


def test_choose_deal_ambiguous():
    assert zoom_sync.choose_deal(["1", "2"]) == (None, "ambiguous")


def test_choose_deal_dedupes_before_deciding():
    assert zoom_sync.choose_deal(["7", "7"]) == ("7", "matched")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'choose_deal'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
def choose_deal(candidate_deal_ids):
    """Decide which deal to attach to. Never guesses.
    Returns (deal_id, reason): ("<id>", "matched") | (None, "no_deal") | (None, "ambiguous")."""
    uniq = list(dict.fromkeys(str(d) for d in candidate_deal_ids if d))
    if len(uniq) == 1:
        return uniq[0], "matched"
    if not uniq:
        return None, "no_deal"
    return None, "ambiguous"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): deal-choice decision (never guesses)"
```

---

### Task 7: Thin Zoom HTTP wrappers (list recordings, fetch transcript)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

These are thin wrappers over `requests`; we test argument shaping with a fake HTTP callable rather than hitting Zoom.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_list_account_recordings_builds_request():
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"meetings": [{"uuid": "u1"}]}

    def fake_get(url, **kwargs):
        captured['url'] = url
        captured['headers'] = kwargs.get('headers')
        captured['params'] = kwargs.get('params')
        return FakeResp()

    out = zoom_sync.list_account_recordings("AT", from_date="2026-04-28", to_date="2026-05-28", http_get=fake_get)
    assert out == [{"uuid": "u1"}]
    assert captured['url'] == "https://api.zoom.us/v2/accounts/me/recordings"
    assert captured['headers']["Authorization"] == "Bearer AT"
    assert captured['params']["from"] == "2026-04-28"
    assert captured['params']["to"] == "2026-05-28"


def test_fetch_transcript_returns_text_for_transcript_file():
    rec = {"recording_files": [
        {"file_type": "MP4", "download_url": "https://zoom.us/rec/vid"},
        {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/rec/t.vtt"},
    ]}

    class FakeResp:
        status_code = 200
        text = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n<v A>hello"

    out = zoom_sync.fetch_transcript(rec, "AT", http_get=lambda url, **k: FakeResp())
    assert "A: hello" in out


def test_fetch_transcript_none_when_no_transcript_file():
    rec = {"recording_files": [{"file_type": "MP4", "download_url": "x"}]}
    assert zoom_sync.fetch_transcript(rec, "AT", http_get=lambda *a, **k: None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'list_account_recordings'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
import requests

ZOOM_API = "https://api.zoom.us/v2"


def list_account_recordings(access_token, from_date, to_date, http_get=requests.get):
    """List account recordings between from_date and to_date (YYYY-MM-DD).
    Returns the list of meeting recording objects (may be empty)."""
    resp = http_get(
        f"{ZOOM_API}/accounts/me/recordings",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"from": from_date, "to": to_date, "page_size": 300},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[zoom-sync] list recordings failed: {resp.status_code} {getattr(resp,'text','')}")
        return []
    return resp.json().get("meetings", [])


def fetch_transcript(recording, access_token, http_get=requests.get):
    """Find the TRANSCRIPT recording file, download it, return parsed plain text.
    Returns None if there is no transcript file or the download fails."""
    files = recording.get("recording_files", []) or []
    tfile = next((f for f in files if f.get("file_type") == "TRANSCRIPT"), None)
    if not tfile:
        return None
    url = tfile.get("download_url")
    resp = http_get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60)
    if resp is None or resp.status_code != 200:
        return None
    return parse_vtt(resp.text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): thin Zoom recording + transcript HTTP wrappers"
```

---

### Task 8: HubSpot HTTP wrappers (resolve deal, write note, patch meeting body)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

These wrappers use the module-level `HS` headers (set from env at import). For tests we inject a fake `http` object exposing `get/post/put/patch`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

class FakeHTTP:
    """Records calls; returns queued responses keyed by (method, url-substring)."""
    def __init__(self, routes):
        self.routes = routes  # list of (method, substr, status, json_obj)
        self.calls = []

    def _resp(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, sub, status, obj in self.routes:
            if m == method and sub in url:
                class R:
                    status_code = status
                    def json(self_inner): return obj
                return R()
        class R404:
            status_code = 404
            def json(self_inner): return {}
        return R404()

    def get(self, url, **kw): return self._resp("GET", url, **kw)
    def post(self, url, **kw): return self._resp("POST", url, **kw)
    def put(self, url, **kw): return self._resp("PUT", url, **kw)
    def patch(self, url, **kw): return self._resp("PATCH", url, **kw)


def test_deals_for_meeting_returns_ids():
    http = FakeHTTP([("GET", "/meetings/55/associations/deals", 200,
                      {"results": [{"toObjectId": "900"}, {"toObjectId": "901"}]})])
    assert zoom_sync.deals_for_meeting("55", http=http) == ["900", "901"]


def test_create_deal_note_associates_to_deal():
    http = FakeHTTP([
        ("POST", "/crm/v3/objects/notes", 201, {"id": "n1"}),
        ("PUT", "/notes/n1/associations/default/deals/900", 200, {}),
    ])
    nid = zoom_sync.create_deal_note("900", "body text", http=http)
    assert nid == "n1"
    assert any(m == "PUT" and "/notes/n1/associations/default/deals/900" in u
               for (m, u, _) in http.calls)


def test_prepend_meeting_recording_links_patches_body():
    http = FakeHTTP([
        ("GET", "/crm/v3/objects/meetings/55", 200, {"properties": {"hs_meeting_body": "old"}}),
        ("PATCH", "/crm/v3/objects/meetings/55", 200, {}),
    ])
    zoom_sync.prepend_meeting_recording_links("55", "https://zoom.us/rec/x", http=http)
    patch_call = next(c for c in http.calls if c[0] == "PATCH")
    sent_body = patch_call[2]["json"]["properties"]["hs_meeting_body"]
    assert "https://zoom.us/rec/x" in sent_body
    assert "old" in sent_body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'deals_for_meeting'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
import os
import time

HS_API_KEY = os.environ.get("HS_API_KEY", "")
HS = {"Authorization": f"Bearer {HS_API_KEY}", "Content-Type": "application/json"}
HS_API = "https://api.hubapi.com"


def deals_for_meeting(meeting_id, http=requests):
    r = http.get(f"{HS_API}/crm/v4/objects/meetings/{meeting_id}/associations/deals",
                 headers=HS, timeout=30)
    if r.status_code != 200:
        return []
    return [str(x["toObjectId"]) for x in r.json().get("results", [])]


def create_deal_note(deal_id, body_text, http=requests):
    """Create a Note with body_text and associate it to the deal. Returns note id or None."""
    r = http.post(f"{HS_API}/crm/v3/objects/notes", headers=HS, json={
        "properties": {"hs_timestamp": str(int(time.time() * 1000)), "hs_note_body": body_text}
    }, timeout=30)
    if r.status_code not in (200, 201):
        return None
    note_id = r.json()["id"]
    http.put(f"{HS_API}/crm/v4/objects/notes/{note_id}/associations/default/deals/{deal_id}",
             headers=HS, timeout=30)
    return note_id


def prepend_meeting_recording_links(meeting_id, recording_url, http=requests):
    """Prepend the recording link to the meeting engagement's body (best-effort)."""
    rg = http.get(f"{HS_API}/crm/v3/objects/meetings/{meeting_id}?properties=hs_meeting_body",
                  headers=HS, timeout=30)
    old = ""
    if rg.status_code == 200:
        old = (rg.json().get("properties", {}) or {}).get("hs_meeting_body") or ""
    new_body = f"<b>Zoom recording:</b> {recording_url}<br>{old}"
    http.patch(f"{HS_API}/crm/v3/objects/meetings/{meeting_id}", headers=HS,
               json={"properties": {"hs_meeting_body": new_body}}, timeout=30)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): HubSpot wrappers — deals_for_meeting, create_deal_note, patch meeting body"
```

---

### Task 9: HubSpot deal resolution (layered matching)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

`resolve_deal` runs the layered match. It depends on smaller wrappers we inject in tests: `find_meeting_id`, `deals_for_meeting`, `open_deals_for_emails`. We test the orchestration, not the HTTP.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_resolve_deal_via_meeting_match():
    deps = {
        "find_meeting_id": lambda rec: "55",
        "deals_for_meeting": lambda mid: ["900"],
        "open_deals_for_emails": lambda emails: [],
    }
    deal_id, meeting_id, reason = zoom_sync.resolve_deal({"uuid": "u"}, deps)
    assert (deal_id, meeting_id, reason) == ("900", "55", "matched")


def test_resolve_deal_falls_back_to_email_when_no_meeting():
    deps = {
        "find_meeting_id": lambda rec: None,
        "deals_for_meeting": lambda mid: [],
        "open_deals_for_emails": lambda emails: ["777"],
    }
    deal_id, meeting_id, reason = zoom_sync.resolve_deal({"uuid": "u"}, deps)
    assert (deal_id, meeting_id, reason) == ("777", None, "matched")


def test_resolve_deal_ambiguous_email_returns_none():
    deps = {
        "find_meeting_id": lambda rec: None,
        "deals_for_meeting": lambda mid: [],
        "open_deals_for_emails": lambda emails: ["1", "2"],
    }
    deal_id, meeting_id, reason = zoom_sync.resolve_deal({"uuid": "u"}, deps)
    assert (deal_id, meeting_id, reason) == (None, None, "ambiguous")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'resolve_deal'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
def resolve_deal(recording, deps):
    """Layered match. Returns (deal_id|None, meeting_id|None, reason).
    deps: dict of injected callables — find_meeting_id, deals_for_meeting,
    open_deals_for_emails."""
    meeting_id = deps["find_meeting_id"](recording)
    if meeting_id:
        deal_id, reason = choose_deal(deps["deals_for_meeting"](meeting_id))
        if reason == "matched":
            return deal_id, meeting_id, "matched"
    # Email fallback
    emails = [p.get("user_email") or p.get("email")
              for p in (recording.get("participants") or [])]
    emails = [e.lower() for e in emails if e and not e.lower().endswith("@furtherai.com")]
    deal_id, reason = choose_deal(deps["open_deals_for_emails"](emails))
    return deal_id, meeting_id, reason
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (20 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): layered deal resolution (meeting match → email fallback)"
```

---

### Task 10: HTTP-backed match helpers (`find_meeting_id`, `open_deals_for_emails`)

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

`find_meeting_id` searches HubSpot meetings by the Zoom ID in `hs_meeting_location`, falling back to host email + time window. `open_deals_for_emails` resolves contacts → open deals.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py
OPEN_STAGES_PRESENT = True

def test_find_meeting_id_by_zoom_id_in_location():
    http = FakeHTTP([
        ("POST", "/crm/v3/objects/meetings/search", 200,
         {"results": [{"id": "55", "properties": {"hs_meeting_location": "https://us02web.zoom.us/j/85512345678"}}]}),
    ])
    rec = {"id": 85512345678, "start_time": "2026-05-20T17:00:00Z", "host_email": "ae@furtherai.com"}
    assert zoom_sync.find_meeting_id(rec, http=http) == "55"


def test_open_deals_for_emails_collects_open_deal_ids():
    http = FakeHTTP([
        ("POST", "/crm/v3/objects/contacts/search", 200, {"results": [{"id": "c1"}]}),
        ("GET", "/contacts/c1/associations/deals", 200, {"results": [{"toObjectId": "900"}]}),
        ("POST", "/crm/v3/objects/deals/search", 200, {"results": [{"id": "900"}]}),
    ])
    assert zoom_sync.open_deals_for_emails(["prospect@acme.com"], http=http) == ["900"]


def test_open_deals_for_emails_empty_when_no_emails():
    assert zoom_sync.open_deals_for_emails([], http=FakeHTTP([])) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'find_meeting_id'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
from datetime import datetime, timezone, timedelta

OPEN_STAGES = ["appointmentscheduled", "qualifiedtobuy", "presentationscheduled",
               "decisionmakerboughtin", "contractsent"]


def _zoom_start_ms(recording):
    s = recording.get("start_time")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def find_meeting_id(recording, http=requests, window_min=10):
    """Find the HubSpot meeting engagement for a Zoom recording.
    1) Zoom meeting ID present in hs_meeting_location. 2) host email + start-time window."""
    zoom_id = extract_zoom_meeting_id(str(recording.get("id", "")))
    if zoom_id:
        r = http.post(f"{HS_API}/crm/v3/objects/meetings/search", headers=HS, json={
            "filterGroups": [{"filters": [
                {"propertyName": "hs_meeting_location", "operator": "CONTAINS_TOKEN", "value": zoom_id}]}],
            "properties": ["hs_meeting_location"], "limit": 5,
        }, timeout=30)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                return str(results[0]["id"])
    # Fallback: host email + start-time window
    start_ms = _zoom_start_ms(recording)
    host = (recording.get("host_email") or "").lower()
    if start_ms and host:
        lo = start_ms - window_min * 60 * 1000
        hi = start_ms + window_min * 60 * 1000
        r = http.post(f"{HS_API}/crm/v3/objects/meetings/search", headers=HS, json={
            "filterGroups": [{"filters": [
                {"propertyName": "hs_meeting_start_time", "operator": "BETWEEN", "value": lo, "highValue": hi}]}],
            "properties": ["hs_meeting_start_time"], "limit": 10,
        }, timeout=30)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if len(results) == 1:
                return str(results[0]["id"])
    return None


def _contact_ids_for_emails(emails, http=requests):
    ids = []
    for email in emails:
        r = http.post(f"{HS_API}/crm/v3/objects/contacts/search", headers=HS, json={
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email"], "limit": 1,
        }, timeout=30)
        if r.status_code == 200:
            for c in r.json().get("results", []):
                ids.append(str(c["id"]))
    return ids


def open_deals_for_emails(emails, http=requests):
    """Resolve emails → contacts → their associated OPEN deals. Returns unique ids."""
    if not emails:
        return []
    deal_ids = []
    for cid in _contact_ids_for_emails(emails, http=http):
        r = http.get(f"{HS_API}/crm/v4/objects/contacts/{cid}/associations/deals",
                     headers=HS, timeout=30)
        if r.status_code != 200:
            continue
        cand = [str(x["toObjectId"]) for x in r.json().get("results", [])]
        if not cand:
            continue
        rs = http.post(f"{HS_API}/crm/v3/objects/deals/search", headers=HS, json={
            "filterGroups": [{"filters": [
                {"propertyName": "hs_object_id", "operator": "IN", "values": cand},
                {"propertyName": "dealstage", "operator": "IN", "values": OPEN_STAGES}]}],
            "properties": ["dealstage"], "limit": 100,
        }, timeout=30)
        if rs.status_code == 200:
            deal_ids.extend(str(d["id"]) for d in rs.json().get("results", []))
    return list(dict.fromkeys(deal_ids))
```

> NOTE for the implementer: `OPEN_STAGES` here are HubSpot default pipeline internal names. Verify the real stage internal names in this portal (Settings → Deals → Pipelines, or `scheduled_deal_sync.py`'s `OPEN_STAGES`) and replace before go-live. The matching test above does not depend on the specific values.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (23 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): HTTP-backed meeting + open-deal match helpers"
```

---

### Task 11: AI summary wrapper

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_summarize_transcript_uses_client_and_returns_text():
    class FakeBlock:
        text = "Summary: discussed pricing. Next steps: send proposal."
    class FakeMsg:
        content = [FakeBlock()]
    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                FakeClient.last = kwargs
                return FakeMsg()
    out = zoom_sync.summarize_transcript(FakeClient, "Zain: hi\nProspect: pricing?")
    assert "discussed pricing" in out
    assert "claude" in FakeClient.last["model"]


def test_summarize_transcript_empty_returns_placeholder():
    assert zoom_sync.summarize_transcript(None, "") == "(no transcript available)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'summarize_transcript'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
SUMMARY_MODEL = "claude-opus-4-8"
SUMMARY_PROMPT = (
    "Summarize this sales call transcript for a CRM note. Be concise. Cover: "
    "who attended, topics discussed, objections/sentiment, and concrete next steps.\n\n"
    "Transcript:\n{transcript}"
)


def summarize_transcript(anthropic_client, transcript_text):
    """Return a short Claude summary of the transcript. Falls back gracefully."""
    if not transcript_text:
        return "(no transcript available)"
    msg = anthropic_client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": SUMMARY_PROMPT.format(transcript=transcript_text[:50000])}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content).strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (25 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): Claude transcript summary wrapper"
```

---

### Task 12: Orchestrator — `process_recording`

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

Ties everything together with injected deps so it is fully testable. Returns a status string: `"matched"`, `"no_deal"`, `"ambiguous"`, `"no_transcript"`, or `"error"`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def _base_deps(**over):
    deps = {
        "get_transcript": lambda rec: "Zain: hi\nProspect: pricing",
        "resolve": lambda rec: ("900", "55", "matched"),
        "summarize": lambda txt: "summary here",
        "create_note": lambda deal_id, body: "n1",
        "patch_meeting": lambda mid, url: None,
        "log": lambda msg: None,
    }
    deps.update(over)
    return deps


def test_process_recording_matched_writes_note():
    written = {}
    deps = _base_deps(create_note=lambda d, b: written.setdefault("deal", d) or "n1")
    rec = {"uuid": "u1", "share_url": "https://zoom.us/rec/x"}
    assert zoom_sync.process_recording(rec, deps) == "matched"
    assert written["deal"] == "900"


def test_process_recording_no_transcript_skips():
    deps = _base_deps(get_transcript=lambda rec: None)
    assert zoom_sync.process_recording({"uuid": "u1"}, deps) == "no_transcript"


def test_process_recording_ambiguous_logs_and_skips_note():
    note_calls = []
    deps = _base_deps(resolve=lambda rec: (None, None, "ambiguous"),
                      create_note=lambda d, b: note_calls.append(d))
    assert zoom_sync.process_recording({"uuid": "u1"}, deps) == "ambiguous"
    assert note_calls == []


def test_process_recording_swallows_errors():
    def boom(rec): raise RuntimeError("zoom down")
    deps = _base_deps(get_transcript=boom)
    assert zoom_sync.process_recording({"uuid": "u1"}, deps) == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'process_recording'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
def process_recording(recording, deps):
    """Process one Zoom recording. Returns a status string. Never raises."""
    uuid = recording.get("uuid", "?")
    try:
        transcript = deps["get_transcript"](recording)
        if not transcript:
            deps["log"](f"[zoom-sync] {uuid}: no transcript — skipping")
            return "no_transcript"
        deal_id, meeting_id, reason = deps["resolve"](recording)
        if reason != "matched":
            deps["log"](f"[zoom-sync] {uuid}: {reason} — logged, not attached")
            return reason
        recording_url = recording.get("share_url") or recording.get("play_url") or ""
        transcript_url = recording_url
        summary = deps["summarize"](transcript)
        body = build_note_body(summary, recording_url, transcript_url, transcript)
        deps["create_note"](deal_id, body)
        if meeting_id:
            deps["patch_meeting"](meeting_id, recording_url)
        deps["log"](f"[zoom-sync] {uuid}: attached to deal {deal_id}")
        return "matched"
    except Exception as e:
        deps["log"](f"[zoom-sync] {uuid}: error {e}")
        return "error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (29 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): orchestrator process_recording (never raises)"
```

---

### Task 13: Run-once driver + loop + backfill CLI

**Files:**
- Modify: `zoom_sync.py`
- Test: `tests/test_zoom_sync.py`

`run_once(from_date, to_date)` wires real deps, iterates recordings, skips already-processed UUIDs, and records processed ones. `zoom_sync_loop()` calls it every 30 min over a short trailing window. `__main__` supports `--backfill-days N`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_zoom_sync.py

def test_run_once_skips_already_processed(monkeypatch, tmp_path):
    state_path = str(tmp_path / "s.json")
    import zoom_auth
    zoom_auth.save_state(state_path, {"refresh_token": "rt",
                                       "processed_recording_uuids": ["seen"]})
    monkeypatch.setattr(zoom_sync, "STATE_PATH", state_path)
    monkeypatch.setattr(zoom_sync, "ZOOM_CLIENT_ID", "cid")
    monkeypatch.setattr(zoom_sync, "ZOOM_CLIENT_SECRET", "sec")
    monkeypatch.setattr(zoom_auth, "get_access_token", lambda *a, **k: "AT")
    monkeypatch.setattr(zoom_sync, "list_account_recordings",
                        lambda *a, **k: [{"uuid": "seen"}, {"uuid": "fresh"}])
    processed = []
    monkeypatch.setattr(zoom_sync, "process_recording",
                        lambda rec, deps: processed.append(rec["uuid"]) or "matched")

    n = zoom_sync.run_once("2026-04-28", "2026-05-28")

    assert processed == ["fresh"]          # "seen" skipped
    assert n == 1
    state = zoom_auth.load_state(state_path)
    assert "fresh" in state["processed_recording_uuids"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: FAIL — `AttributeError: module 'zoom_sync' has no attribute 'run_once'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to zoom_sync.py
import sys
import zoom_auth

STATE_PATH = os.environ.get("ZOOM_STATE_PATH", "/data/zoom_state.json")
ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")


def _build_deps(access_token, anthropic_client):
    def get_transcript(rec):
        return fetch_transcript(rec, access_token)

    def resolve(rec):
        return resolve_deal(rec, {
            "find_meeting_id": lambda r: find_meeting_id(r),
            "deals_for_meeting": lambda mid: deals_for_meeting(mid),
            "open_deals_for_emails": lambda emails: open_deals_for_emails(emails),
        })

    return {
        "get_transcript": get_transcript,
        "resolve": resolve,
        "summarize": lambda txt: summarize_transcript(anthropic_client, txt),
        "create_note": lambda deal_id, body: create_deal_note(deal_id, body),
        "patch_meeting": lambda mid, url: prepend_meeting_recording_links(mid, url),
        "log": print,
    }


def run_once(from_date, to_date, anthropic_client=None):
    """Process all recordings in [from_date, to_date] not already processed.
    Returns the number processed this run."""
    if anthropic_client is None:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    access_token = zoom_auth.get_access_token(STATE_PATH, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET)
    state = zoom_auth.load_state(STATE_PATH)
    seen = set(state.get("processed_recording_uuids", []))
    deps = _build_deps(access_token, anthropic_client)
    count = 0
    for rec in list_account_recordings(access_token, from_date, to_date):
        uuid = rec.get("uuid")
        if not uuid or uuid in seen:
            continue
        process_recording(rec, deps)
        seen.add(uuid)
        count += 1
        # persist progress incrementally so a crash doesn't reprocess
        state["processed_recording_uuids"] = sorted(seen)
        zoom_auth.save_state(STATE_PATH, state)
    print(f"[zoom-sync] run complete — processed {count} new recording(s)")
    return count


def zoom_sync_loop():
    """Daemon loop: every 30 min, process recordings from the last 2 days."""
    time.sleep(90)  # let the bot settle on boot
    while True:
        try:
            today = datetime.now(timezone.utc).date()
            frm = (today - timedelta(days=2)).isoformat()
            run_once(frm, today.isoformat())
        except Exception as e:
            print(f"[zoom-sync] loop error: {e}")
        time.sleep(30 * 60)


if __name__ == "__main__":
    if "--backfill-days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--backfill-days") + 1])
        today = datetime.now(timezone.utc).date()
        run_once((today - timedelta(days=days)).isoformat(), today.isoformat())
    else:
        run_once(
            (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat(),
            datetime.now(timezone.utc).date().isoformat(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -m pytest tests/test_zoom_sync.py -v`
Expected: PASS (30 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add zoom_sync.py tests/test_zoom_sync.py
git commit -m "feat(zoom): run_once driver, 30-min loop, --backfill-days CLI"
```

---

### Task 14: One-time authorization helper

**Files:**
- Create: `authorize_zoom.py`

No automated test (interactive, network-bound, run once). Verified manually in Task 16.

- [ ] **Step 1: Write the implementation**

```python
# authorize_zoom.py
"""One-time Zoom OAuth authorization (General OAuth app).

Run on the machine whose browser will approve, logged into Zoom as an ACCOUNT
ADMIN so the token carries account-wide recording scopes.

Usage:
    ZOOM_CLIENT_ID=... ZOOM_CLIENT_SECRET=... ZOOM_STATE_PATH=./zoom_state.json \\
        python3 authorize_zoom.py

Opens the consent URL, captures the ?code= on localhost:3000/zoom/callback,
exchanges it, and writes the refresh token into the state file.
"""
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

import requests
import zoom_auth

CLIENT_ID = os.environ["ZOOM_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOOM_CLIENT_SECRET"]
STATE_PATH = os.environ.get("ZOOM_STATE_PATH", "./zoom_state.json")
REDIRECT_URI = "http://localhost:3000/zoom/callback"

_code = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        if "code" in qs:
            _code["code"] = qs["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Zoom authorized. You can close this tab.")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    auth_url = "https://zoom.us/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
    print(f"Opening:\n{auth_url}\n(Log in as an ACCOUNT ADMIN to grant account-wide scopes.)")
    webbrowser.open(auth_url)
    server = HTTPServer(("localhost", 3000), Handler)
    while "code" not in _code:
        server.handle_request()
    resp = requests.post("https://zoom.us/oauth/token",
                         data={"grant_type": "authorization_code", "code": _code["code"],
                               "redirect_uri": REDIRECT_URI},
                         auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)
    resp.raise_for_status()
    refresh = resp.json()["refresh_token"]
    state = zoom_auth.load_state(STATE_PATH)
    state["refresh_token"] = refresh
    state.setdefault("processed_recording_uuids", [])
    zoom_auth.save_state(STATE_PATH, state)
    print(f"Success. Refresh token written to {STATE_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check it imports**

Run: `cd ~/meetingbuddy && ZOOM_CLIENT_ID=x ZOOM_CLIENT_SECRET=y python3 -c "import authorize_zoom; print('ok')"`
Expected: prints `ok` (no network call on import)

- [ ] **Step 3: Commit**

```bash
cd ~/meetingbuddy
git add authorize_zoom.py
git commit -m "feat(zoom): one-time admin authorization helper"
```

---

### Task 15: Wire the loop into the bot + env docs

**Files:**
- Modify: `meeting_bot.py` (the `if __name__ == '__main__':` block, ~line 1332)
- Modify: `.env.example`

- [ ] **Step 1: Add the import near the other module imports**

In `meeting_bot.py`, alongside `import sheet_sync` (~line 36), add:

```python
import zoom_sync
```

- [ ] **Step 2: Start the daemon thread**

In the `if __name__ == '__main__':` block, after the `live_sweep_loop` thread start, add:

```python
    threading.Thread(target=zoom_sync.zoom_sync_loop, daemon=True).start()
    print('[zoom-sync] background poll started (every 30 min, last 2 days)')
```

- [ ] **Step 3: Update `.env.example`**

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ANTHROPIC_API_KEY=sk-ant-...
HS_API_KEY=pat-na2-...
ZOOM_CLIENT_ID=...
ZOOM_CLIENT_SECRET=...
ZOOM_STATE_PATH=/data/zoom_state.json
```

- [ ] **Step 4: Verify the bot module still imports**

Run: `cd ~/meetingbuddy && python3 -c "import ast; ast.parse(open('meeting_bot.py').read()); print('syntax ok')"`
Expected: prints `syntax ok`

- [ ] **Step 5: Run the full test suite**

Run: `cd ~/meetingbuddy && python3 -m pytest -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
cd ~/meetingbuddy
git add meeting_bot.py .env.example
git commit -m "feat(zoom): wire zoom_sync_loop into bot + env docs"
```

---

### Task 16: Manual go-live (coordination — no code)

Not an automated task; the checklist to actually turn it on.

- [ ] Confirm Zoom app scopes in the app's scope picker: account recording read + transcript read + user read. Record the exact scope strings.
- [ ] Confirm cloud recording + audio transcript are enabled on the Zoom account.
- [ ] In Railway: add a persistent volume mounted at `/data` on the bot service; set `ZOOM_STATE_PATH=/data/zoom_state.json`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`.
- [ ] Run `authorize_zoom.py` once with an admin Zoom login (Shariq, or Zain after Shariq grants admin role). Confirm the refresh token lands in the state file, then copy/seed it into the Railway volume's `zoom_state.json`.
- [ ] Replace `OPEN_STAGES` in `zoom_sync.py` with this portal's real deal-stage internal names (cross-check `scheduled_deal_sync.py`).
- [ ] Verify the GCal→HubSpot meeting assumption on one real synced meeting: does `hs_meeting_location` contain the Zoom meeting ID? If not, the exact-match path won't fire and matching leans on host+time / email.
- [ ] Run the backfill once: `python3 zoom_sync.py --backfill-days 30`; review the log for matched vs. logged-unmatched counts.
- [ ] Deploy; watch `[zoom-sync]` logs for one live cycle.
- [ ] **Rotate the Zoom Client Secret** (it was shared in plaintext over Slack/chat), update the Railway env var, and re-run `authorize_zoom.py` if the rotation invalidates the app credentials.

---

## Self-Review

**Spec coverage:**
- Poll account-wide recordings every ~30 min → Tasks 7, 13 (`list_account_recordings`, `zoom_sync_loop`). ✓
- Runs in `meeting_bot.py` on Railway → Task 15. ✓
- Claude summary + recording/transcript links + full transcript text on the deal → Tasks 5, 11, 12 (`build_note_body`, `summarize_transcript`, `process_recording`). ✓
- Matching: Zoom-ID-in-meeting → host+time → email→contact→deal → log & skip → Tasks 6, 9, 10. ✓
- Never guess (ambiguous → skip) → Tasks 6 (`choose_deal`), 9, 12. ✓
- Unmatched = log only (no Slack) → Task 12 (`deps["log"]` only). ✓
- Note on deal + links on meeting card (via `hs_meeting_body` patch) → Task 8 (`prepend_meeting_recording_links`), Task 12. ✓
- Rotating refresh token persisted → Task 2. ✓
- State on Railway volume; idempotent processed-UUID set → Tasks 1, 13; volume in Task 16. ✓
- One-time admin authorization helper → Task 14. ✓
- 30-day backfill → Task 13 (`--backfill-days`). ✓
- Rotate Client Secret → Task 16. ✓
- Reliability: per-recording try/except, loop never crashes bot → Task 12 (`process_recording` never raises), Task 13 (loop wraps in try/except). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. `OPEN_STAGES` and exact scope strings are explicitly flagged as portal-specific values to verify in Task 16, with the note that tests don't depend on them.

**Type/name consistency:** Function names are consistent across tasks — `load_state`/`save_state`/`get_access_token` (zoom_auth); `parse_vtt`, `extract_zoom_meeting_id`, `build_note_body`, `choose_deal`, `list_account_recordings`, `fetch_transcript`, `deals_for_meeting`, `create_deal_note`, `prepend_meeting_recording_links`, `resolve_deal`, `find_meeting_id`, `open_deals_for_emails`, `summarize_transcript`, `process_recording`, `run_once`, `zoom_sync_loop` (zoom_sync). `resolve_deal` deps keys (`find_meeting_id`, `deals_for_meeting`, `open_deals_for_emails`) match `_build_deps`. `process_recording` deps keys (`get_transcript`, `resolve`, `summarize`, `create_note`, `patch_meeting`, `log`) match `_build_deps` and the tests.
