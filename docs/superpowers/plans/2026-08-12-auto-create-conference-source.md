# Auto-create HubSpot event sources — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `#conference-meetings` booking names a conference with no matching HubSpot `conference_source` option, the bot canonicalizes the name (best-effort web lookup), creates the dropdown option, and files the meeting to it — instead of dumping it in `other`.

**Architecture:** Add a `conference_name_raw` field to the LLM extraction so unknown events surface. A resolve-or-create step canonicalizes the raw name, dedups against the live HubSpot option list, creates the option via a PATCH of the full options array (under a lock), and posts a Slack confirm/rename notice. Wired into `_process_booking` before the existing conference handling; only fires when the parsed `conference_source` is `None`/`other`.

**Tech Stack:** Python 3, Flask + Slack Bolt, `requests` (HubSpot REST), the existing Anthropic `client` (web-search server tool `web_search_20250305`), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-auto-create-conference-source-design.md`.
- New bucket **value**: `<name-slug>_<year>` (e.g. `broker_tech_conference_2026`). **label**: `<Canonical Name> <year>` — **year always kept**.
- Web lookup is **best-effort, confident-only**: use the looked-up name ONLY if the model returns `confident: true`; on any error/low-confidence, fall back to the BDR's raw text. Never let a lookup failure raise.
- Slack notice is posted on **every** auto-create (confirm/rename backstop).
- Dedup collapses aliases/variants of the same event+year onto one existing option — never create when a normalized match exists.
- Creating an option must PATCH the **full** existing options array (HubSpot replaces the list); preserve all existing options including hidden ones. Guard concurrent creates with a lock + re-fetch inside the lock.
- Commit messages: subject line only, no body, no Co-Authored-By (per user global rules).
- HubSpot meetings property: `conference_source` on object type `meetings`. Portal token is `HS_API_KEY` (Railway env). Property REST base: `https://api.hubapi.com/crm/v3/properties/meetings/conference_source`.

---

### Task 1: `slugify_conference` pure function

**Files:**
- Modify: `meeting_bot.py` (add near `detect_conference_from_title`, ~line 318)
- Test: `tests/test_conference_autocreate.py` (create)

**Interfaces:**
- Produces: `slugify_conference(name: str, year) -> tuple[str, str] | None` — returns `(value, label)` or `None` for junk. `value = "<name-slug>_<year>"`, `label = "<Name> <year>"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conference_autocreate.py
import meeting_bot as mb


def test_slugify_basic():
    assert mb.slugify_conference("Broker Tech Conference", 2026) == (
        "broker_tech_conference_2026", "Broker Tech Conference 2026")

def test_slugify_acronym_and_punctuation():
    assert mb.slugify_conference("BTC", 2026) == ("btc_2026", "BTC 2026")
    assert mb.slugify_conference("R.I.M.S. RiskWorld!", 2026) == (
        "r_i_m_s_riskworld_2026", "R.I.M.S. RiskWorld! 2026")

def test_slugify_junk_returns_none():
    assert mb.slugify_conference("", 2026) is None
    assert mb.slugify_conference("   ", 2026) is None
    assert mb.slugify_conference("2026", 2026) is None   # all-digit name
    assert mb.slugify_conference("Booth", None) is None  # no year → can't scope
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/nvme/PROMETHEUS/WORK/FAI/meetingbuddy && python -m pytest tests/test_conference_autocreate.py -q`
Expected: FAIL — `AttributeError: module 'meeting_bot' has no attribute 'slugify_conference'`

- [ ] **Step 3: Write minimal implementation**

```python
# meeting_bot.py — add after detect_conference_from_title (~line 318)
def slugify_conference(name, year):
    """(value, label) for a new conference bucket, or None for junk.
    value = '<name-slug>_<year>', label = '<Name> <year>'. Year always kept."""
    if not name or not year:
        return None
    clean = name.strip()
    core = re.sub(r'[^a-z0-9]+', '_', clean.lower()).strip('_')
    if not core or core.isdigit():
        return None
    value = f'{core}_{year}'[:50]
    label = f'{clean} {year}'
    return value, label
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_conference_autocreate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_autocreate.py
git commit -m "feat(conf): slugify_conference helper for new event buckets"
```

---

### Task 2: `canonicalize_conference` best-effort web lookup

**Files:**
- Modify: `meeting_bot.py` (add after `slugify_conference`)
- Test: `tests/test_conference_autocreate.py`

**Interfaces:**
- Consumes: module-level `client` (Anthropic), already defined near top of `meeting_bot.py`.
- Produces: `canonicalize_conference(raw: str, meeting_date) -> dict | None` → `{'name': str, 'year': int}`. Best-effort: returns the raw-fallback dict when the web lookup is absent/unsure, `None` only when it can't even derive a year.

- [ ] **Step 1: Write the failing test** (fallback path only — no live web call)

```python
def test_canonicalize_fallback_when_no_client(monkeypatch):
    monkeypatch.setattr(mb, "client", None)   # simulate no LLM available
    out = mb.canonicalize_conference("BTC 2026", "2026-09-01")
    assert out == {"name": "BTC 2026", "year": 2026}

def test_canonicalize_year_from_meeting_date(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    out = mb.canonicalize_conference("Broker Tech Conference", "2027-03-10")
    assert out == {"name": "Broker Tech Conference", "year": 2027}

def test_canonicalize_no_year_returns_none(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    assert mb.canonicalize_conference("Broker Tech Conference", None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conference_autocreate.py -k canonicalize -q`
Expected: FAIL — no attribute `canonicalize_conference`

- [ ] **Step 3: Write minimal implementation**

> First add `import json` to the imports block (~line 26) — the module does not currently import it (it only uses `requests`' `.json()` method).

```python
# meeting_bot.py — after slugify_conference
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
        r = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=1024,
            tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
            system=("You identify insurance/insurtech industry conferences. "
                    "Search the web, then reply with ONLY a JSON object: "
                    '{"name": "<official full name, no year>", "confident": true|false}. '
                    "Set confident=false if you are not sure the acronym maps to a real event."),
            messages=[{'role': 'user',
                       'content': f'What conference is "{raw}" (an insurance industry event)?'}],
        )
        # Server tool loop may pause; re-send up to 3x to let it finish (fixed bound).
        msgs = [{'role': 'user', 'content': f'What conference is "{raw}"?'}]
        hops = 0
        while r.stop_reason == 'pause_turn' and hops < 3:
            hops += 1
            msgs.append({'role': 'assistant', 'content': r.content})
            r = client.messages.create(
                model='claude-opus-4-8', max_tokens=1024,
                tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
                messages=msgs)
        text = ''.join(b.text for b in r.content if getattr(b, 'type', '') == 'text')
        mjson = re.search(r'\{.*\}', text, re.S)
        if mjson:
            data = json.loads(mjson.group(0))
            if data.get('confident') and data.get('name'):
                return {'name': str(data['name']).strip(), 'year': year}
    except Exception as e:
        print(f'[conf] canonicalize failed for {raw!r}: {e}', flush=True)
    return {'name': name, 'year': year}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_conference_autocreate.py -k canonicalize -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_autocreate.py
git commit -m "feat(conf): best-effort web canonicalization of new conference names"
```

---

### Task 3: HubSpot option reader + writer

**Files:**
- Modify: `meeting_bot.py` (add near the other `hs_*` helpers, after `hs_update_meeting`, ~line 447)
- Test: `tests/test_conference_autocreate.py`

**Interfaces:**
- Produces:
  - `hs_conference_options(force=False) -> list[dict]` — cached list of `{'value','label','hidden'}` from the `conference_source` property.
  - `hs_add_conference_option(value: str, label: str) -> bool` — appends the option (full-array PATCH), refreshes the cache, returns success.
- Consumes: module-level `HS` headers dict (already defined ~line 47).

- [ ] **Step 1: Write the failing test** (reader cache + dedup normalization only; no live HTTP)

```python
def test_norm_option_matches_alias():
    # normalization used for dedup: lowercase alnum, drop underscores
    assert mb._norm_opt("Broker Tech Conference 2026") == mb._norm_opt("broker_tech_conference_2026")
    assert mb._norm_opt("WSIA") != mb._norm_opt("WSIA Dinner")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conference_autocreate.py -k norm_opt -q`
Expected: FAIL — no attribute `_norm_opt`

- [ ] **Step 3: Write minimal implementation**

```python
# meeting_bot.py — after hs_update_meeting (~line 447)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_conference_autocreate.py -k norm_opt -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_autocreate.py
git commit -m "feat(conf): read + append conference_source dropdown options"
```

---

### Task 4: `resolve_or_create_conference` (dedup + create)

**Files:**
- Modify: `meeting_bot.py` (add after `hs_add_conference_option`)
- Test: `tests/test_conference_autocreate.py`

**Interfaces:**
- Consumes: `canonicalize_conference`, `slugify_conference`, `hs_conference_options`, `hs_add_conference_option`, `_norm_opt`.
- Produces: `resolve_or_create_conference(raw, meeting_date) -> dict | None` → `{'value': str, 'created': bool, 'label': str}` or `None` when it can't resolve. `created=True` only when a brand-new option was written (caller posts the Slack notice).

- [ ] **Step 1: Write the failing test** (dedup path — existing option, no create)

```python
def test_resolve_dedups_to_existing(monkeypatch):
    monkeypatch.setattr(mb, "client", None)  # force raw fallback
    monkeypatch.setattr(mb, "hs_conference_options",
                        lambda force=False: [{"value": "broker_tech_conference_2026",
                                              "label": "Broker Tech Conference 2026", "hidden": False}])
    created = []
    monkeypatch.setattr(mb, "hs_add_conference_option",
                        lambda v, l: created.append((v, l)) or True)
    out = mb.resolve_or_create_conference("Broker Tech Conference", "2026-09-01")
    assert out == {"value": "broker_tech_conference_2026", "created": False,
                   "label": "Broker Tech Conference 2026"}
    assert created == []   # dedup hit → no option written

def test_resolve_creates_new(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    monkeypatch.setattr(mb, "hs_conference_options", lambda force=False: [])
    created = []
    monkeypatch.setattr(mb, "hs_add_conference_option",
                        lambda v, l: created.append((v, l)) or True)
    out = mb.resolve_or_create_conference("BTC", "2026-09-01")
    assert out == {"value": "btc_2026", "created": True, "label": "BTC 2026"}
    assert created == [("btc_2026", "BTC 2026")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conference_autocreate.py -k resolve -q`
Expected: FAIL — no attribute `resolve_or_create_conference`

- [ ] **Step 3: Write minimal implementation**

```python
# meeting_bot.py — after hs_add_conference_option
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_conference_autocreate.py -k resolve -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add meeting_bot.py tests/test_conference_autocreate.py
git commit -m "feat(conf): resolve-or-create conference bucket with dedup"
```

---

### Task 5: Surface the raw event name from the parser

**Files:**
- Modify: `meeting_bot.py` — `_BOOKING_ITEM` schema (~line 179) and `PARSE_PROMPT` conference rules (~line 146)

**Interfaces:**
- Produces: parsed bookings may include `conference_name_raw: str | None`.

- [ ] **Step 1: Add the schema field**

In `_BOOKING_ITEM['properties']`, after the `conference_source` line (~line 176), add:

```python
        'conference_name_raw': {'type': ['string', 'null']},
```

- [ ] **Step 2: Add the prompt guidance**

In `PARSE_PROMPT`, immediately after the `conference_source rules:` block (after the `ny_dinner` synonym line, ~line 145), add:

```
  - conference_name_raw: the LITERAL event name as written, taken ONLY from an
    explicit "Event Source:" / "Source:" line or a conference header (e.g. "BTC 2026").
    Do NOT infer it from the company name or stray words. Null if none is named.
```

- [ ] **Step 3: Sanity check the module imports**

Run: `python -c "import meeting_bot"`
Expected: no error (imports/env may warn but must not crash on syntax).
> If it exits non-zero due to missing env vars, that's pre-existing — confirm the failure is an env `KeyError`, not a `SyntaxError`.

- [ ] **Step 4: Commit**

```bash
git add meeting_bot.py
git commit -m "feat(conf): extract conference_name_raw for unknown events"
```

---

### Task 6: Wire into `_process_booking` + Slack notice

**Files:**
- Modify: `meeting_bot.py` — `_process_booking`, right after the conference defaults block (~line 795)

**Interfaces:**
- Consumes: `resolve_or_create_conference`, the booking's `say`/`ts`.

- [ ] **Step 1: Insert the resolution + notice**

After the block that defaults `meeting_type` to `'conference'` (ends ~line 795, before the "Fold location into notes" comment), add:

```python
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
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `python -m pytest tests/ -q`
Expected: PASS (existing suite + new file). Note the module import must succeed with test env; if `tests/` set env vars before importing `meeting_bot`, this holds.

- [ ] **Step 3: Manual reasoning check (no code)**

Confirm by reading: both downstream branches (existing-meeting update ~line 857 and new-meeting create ~line 933) read `parsed['conference_source']` / derive `conf` from it, so the resolved value flows to the HubSpot write, the sheet push, and the reply tag with no further change.

- [ ] **Step 4: Commit**

```bash
git add meeting_bot.py
git commit -m "feat(conf): auto-create + file unknown conferences, notify in Slack"
```

---

### Task 7: Close the `tmpcc`/`iiusa` enum gap

**Files:**
- Modify: `meeting_bot.py` — `conference_source` enum in `_BOOKING_ITEM` (~line 176)

- [ ] **Step 1: Add the two values**

In the `conference_source` enum list, add `'tmpcc'` and `'iiusa'` (they already exist in HubSpot and in `_CONF_RULES`). Insert after `'tmpaa'`:

```python
        'conference_source':   {'type': ['string', 'null'], 'enum': ['wsia_uw_summit', 'wsia_dinner', 'insurtech_ny_spring', 'insurtech_insights', 'insurance_innovators', 'tmpaa', 'tmpcc', 'iiusa', 'rims_riskworld', 'nashville_dinner', 'ny_dinner', 'insurance_insider', 'reuters_es', 'reuters_program_managers', 'future_of_insurance', 'insurance_fest', 'other', None]},
```

- [ ] **Step 2: Verify import**

Run: `python -c "import meeting_bot"` (env-permitting) or `python -m pytest tests/ -q`
Expected: no syntax error; tests pass.

- [ ] **Step 3: Commit**

```bash
git add meeting_bot.py
git commit -m "fix(conf): add tmpcc + iiusa to conference_source enum"
```

---

### Task 8: One-off re-stamp of Dani's existing meeting

**Files:**
- Create: `scripts/patch_btc_2026_08_12.py`

**Interfaces:**
- Consumes: `meeting_bot` module (`HS`, `resolve_or_create_conference`, `requests`).

- [ ] **Step 1: Write the one-off script**

```python
# scripts/patch_btc_2026_08_12.py
"""One-off: move meeting 390614433491 (The Best MGA / Dani) from conference_source
'other' to the Broker Tech Conference 2026 bucket, creating the bucket if missing.
Run once on Railway (has HS_API_KEY): `railway run python scripts/patch_btc_2026_08_12.py`."""
import requests
import meeting_bot as mb

MEETING_ID = '390614433491'

def main():
    resolved = mb.resolve_or_create_conference('BTC 2026', '2026-09-01')
    assert resolved, 'could not resolve BTC 2026'
    value = resolved['value']
    r = requests.patch(
        f'https://api.hubapi.com/crm/v3/objects/meetings/{MEETING_ID}',
        headers=mb.HS,
        json={'properties': {
            'conference_source': value,
            'hs_meeting_title': f'FurtherAI + The Best MGA [{value}]',
        }},
        timeout=30)
    print('patch', r.status_code, '->', value)
    assert r.status_code == 200, r.text

if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Commit (do not run here — no token on this box)**

```bash
git add scripts/patch_btc_2026_08_12.py
git commit -m "chore(conf): one-off re-stamp Dani BTC meeting to broker_tech_conference_2026"
```

- [ ] **Step 3: Run on Railway (manual, after deploy)**

Run: `railway run python scripts/patch_btc_2026_08_12.py`
Expected: `patch 200 -> broker_tech_conference_2026`

---

## Self-Review

**Spec coverage:**
- §1 schema `conference_name_raw` → Task 5. §2 canonicalize → Task 2. §3 slugify → Task 1. §4 resolve-or-create (dedup + create + lock) → Tasks 3–4. §5 wire → Task 6. §6 Slack notice → Task 6. §7 one-off → Task 8. §8 tmpcc/iiusa → Task 7. All covered.

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `slugify_conference(name, year) -> (value, label)`; `canonicalize_conference -> {'name','year'}`; `resolve_or_create_conference -> {'value','created','label'}`; `hs_conference_options -> list[{'value','label','hidden'}]`; `hs_add_conference_option(value,label) -> bool`. Names match across tasks. `_norm_opt`, `_conf_opts_lock`, `_conf_opts_cache`, `_CONF_PROP_URL` referenced consistently.

**Ambiguity:** dedup matches on normalized value OR normalized label; create overrides `other` only on a concrete result; notice fires only on `created=True`. Explicit.
