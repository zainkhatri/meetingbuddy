# BDR sdr_owner Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sdr_owner` the canonical BDR-attribution field on Contacts + Deals, populated by who actually sourced the meeting (incl. Ben + Matt), with a one-time backfill — implementing the engineering portion of the BDR CRM spec.

**Architecture:** Meeting Bot already writes `meeting_sourced_by` (owner-id) on meetings and maps owner-id → display name via `sheet_sync.OWNER_DISPLAY`. We add a small testable helper to turn an owner-id into the `sdr_owner` enum value, have the bot stamp the sourced contact's `sdr_owner` (fill-if-empty, never overwrite), and ship a one-time backfill script that walks existing meetings and sets `sdr_owner` on their contacts from `meeting_sourced_by`.

**Tech Stack:** Python 3.9, `requests`, HubSpot CRM v3 API (PAT), `pytest`. Repo: `~/meetingbuddy`. Spec: `~/hubspot-cleanup/docs/superpowers/specs/2026-06-01-bdr-crm-process-and-sops-design.md`.

**Scope:** This plan is the code-able slice of the spec (#2 population + backfill). Non-code items are tracked as explicit tasks but are admin/process/docs, not TDD: portal-admin property edits (Task 1), the canonical-field reconciliation decision (Task 6), publishing the two SOPs (Task 7), and `qualified_stage` adoption (Task 8).

---

### Task 1: [PREREQ — Portal admin, manual] Add Ben + Matt to the `sdr_owner` enum

The `sdr_owner` property is a string enumeration whose options are only `Zain / Jacob / Dani`. Writing `Ben` or `Matt` will fail until the options exist. This is done by a HubSpot admin in-app (MCP/PAT write to property schemas is not authorized here).

**Files:** none (HubSpot portal config).

- [ ] **Step 1: Add options on Contacts.** Settings → Properties → Contacts → `SDR Owner` → add options `Ben` and `Matt` (label = value, matching `sheet_sync.OWNER_DISPLAY`).
- [ ] **Step 2: Add the same options on Deals** → `SDR Owner`.
- [ ] **Step 3: Verify via PAT.**

Run:
```bash
curl -s -H "Authorization: Bearer $HS_API_KEY" \
  "https://api.hubapi.com/crm/v3/properties/contacts/sdr_owner" \
  | python3 -c 'import sys,json;print(sorted(o["value"] for o in json.load(sys.stdin)["options"]))'
```
Expected: list includes `Ben` and `Matt` (alongside `Dani`, `Jacob`, `Zain`).

- [ ] **Step 4: Repeat the verify for deals** (`.../properties/deals/sdr_owner`). Expected: same.

> Do NOT start Task 3 (bot writes) or Task 5 (backfill) until this passes — writes of `Ben`/`Matt` will 400 otherwise.

---

### Task 2: `bdr_sdr_owner_value()` helper + tests

A pure, testable mapping from owner-id → `sdr_owner` enum value. Lives in `sheet_sync.py` (importable without env; `meeting_bot.py` is not, since it reads env at import). Reuses the existing `OWNER_DISPLAY` so the roster is defined once.

**Files:**
- Modify: `~/meetingbuddy/sheet_sync.py` (add helper after `OWNER_DISPLAY`)
- Test: `~/meetingbuddy/tests/test_bdr_owner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bdr_owner.py`:
```python
import sheet_sync


def test_known_bdrs_map_to_display_value():
    assert sheet_sync.bdr_sdr_owner_value("88760040") == "Zain"
    assert sheet_sync.bdr_sdr_owner_value("162210484") == "Jacob"
    assert sheet_sync.bdr_sdr_owner_value("82377567") == "Dani"
    assert sheet_sync.bdr_sdr_owner_value("164943105") == "Ben"
    assert sheet_sync.bdr_sdr_owner_value("92184259") == "Matt"


def test_int_owner_id_is_accepted():
    assert sheet_sync.bdr_sdr_owner_value(88760040) == "Zain"


def test_non_bdr_or_blank_returns_empty():
    assert sheet_sync.bdr_sdr_owner_value("654909503") == ""  # Aman (AE/not BDR)
    assert sheet_sync.bdr_sdr_owner_value("") == ""
    assert sheet_sync.bdr_sdr_owner_value(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/meetingbuddy && python3 -W ignore -m pytest tests/test_bdr_owner.py -q`
Expected: FAIL — `AttributeError: module 'sheet_sync' has no attribute 'bdr_sdr_owner_value'`

- [ ] **Step 3: Write minimal implementation**

In `sheet_sync.py`, immediately after the `OWNER_DISPLAY = {...}` block, add:
```python
def bdr_sdr_owner_value(owner_id):
    """Map a HubSpot owner id to the `sdr_owner` enum display value.

    Returns '' for non-BDR owners or blank input, so callers can safely skip
    writing the property. The roster is OWNER_DISPLAY (the same map used for the
    sheet's 'Meeting Sourced By' column), so BDRs are defined in exactly one place.
    """
    return OWNER_DISPLAY.get(str(owner_id or ''), '')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/meetingbuddy && python3 -W ignore -m pytest tests/test_bdr_owner.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add sheet_sync.py tests/test_bdr_owner.py
git commit -m "feat(sdr): add bdr_sdr_owner_value() owner-id -> sdr_owner enum helper"
```

---

### Task 3: Meeting Bot stamps contact `sdr_owner` (create + fill-if-empty)

When the bot logs a BDR-sourced meeting it should set the sourced contact's `sdr_owner` to the sourcing rep, on both newly-created and pre-existing contacts, never overwriting a non-empty value (mirrors the sheet-sync preservation rule). `meeting_bot.py` is not unit-testable (env at import), so this task verifies against a real test contact in HubSpot.

**Files:**
- Modify: `~/meetingbuddy/meeting_bot.py` — `hs_create_contact` (line ~199) and `_process_booking` (line ~589, where the contact is resolved)

- [ ] **Step 1: Add `sdr_owner` to new-contact creation.** In `hs_create_contact` (after the `if owner_id: props['hubspot_owner_id'] = owner_id` line), add:
```python
    sdr_val = sheet_sync.bdr_sdr_owner_value(owner_id)
    if sdr_val:
        props['sdr_owner'] = sdr_val
```

- [ ] **Step 2: Add a fill-if-empty setter for existing contacts.** Add this function near `hs_create_contact`:
```python
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
```

- [ ] **Step 3: Call the setter for resolved contacts.** In `_process_booking`, immediately after `contact_id = contact['id'] if contact else None` (line ~587), add:
```python
    if contact_id:
        hs_set_contact_sdr_owner(contact_id, owner_id)
```
(This covers the existing-contact path; new contacts already get it via Step 1.)

- [ ] **Step 4: Byte-compile to confirm no syntax error.**

Run: `cd ~/meetingbuddy && python3 -W ignore -c 'import py_compile; py_compile.compile("meeting_bot.py", doraise=True); print("OK")'`
Expected: `OK`

- [ ] **Step 5: Manual verification against a real contact.** Pick a recent BDR-sourced meeting's contact id (e.g. from the audit script) and run a fill on a contact whose `sdr_owner` is empty:
```bash
cd ~/meetingbuddy && python3 -W ignore -c '
import os, meeting_bot as mb
# owner_id 88760040 = Zain; replace CONTACT_ID with a real contactId whose sdr_owner is empty
mb.hs_set_contact_sdr_owner("CONTACT_ID", "88760040")
import requests
r=requests.get("https://api.hubapi.com/crm/v3/objects/contacts/CONTACT_ID",headers=mb.HS,params={"properties":"sdr_owner"},timeout=15)
print(r.json()["properties"])
'  # requires bot env (HS_API_KEY etc.) loaded
```
Expected: `{'sdr_owner': 'Zain', ...}`. Then re-run with owner `162210484` (Jacob) and confirm it does **not** overwrite (still `Zain`).

- [ ] **Step 6: Commit**

```bash
cd ~/meetingbuddy
git add meeting_bot.py
git commit -m "feat(sdr): bot stamps contact sdr_owner from sourcing BDR (fill-if-empty)"
```

---

### Task 4: One-time backfill — `sdr_owner` ← `meeting_sourced_by`

Walk existing meetings that carry `meeting_sourced_by`, find their associated non-internal contacts, and fill empty `sdr_owner`. Dry-run first; never overwrite.

**Files:**
- Create: `~/meetingbuddy/scripts/backfill_sdr_owner.py`

- [ ] **Step 1: Write the script.**
```python
#!/usr/bin/env python3
"""One-time backfill: set contact `sdr_owner` from the BDR who sourced their
meeting (`meeting_sourced_by`). Fill-if-empty; never overwrites. Dry-run by default.

Usage:
  HS_API_KEY=... python3 scripts/backfill_sdr_owner.py            # dry-run
  HS_API_KEY=... python3 scripts/backfill_sdr_owner.py --apply
"""
import os, sys, time, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheet_sync

HS = {'Authorization': f'Bearer {os.environ["HS_API_KEY"]}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def search_sourced_meetings():
    after = None
    while True:
        body = {'filterGroups': [{'filters': [
            {'propertyName': 'meeting_sourced_by', 'operator': 'HAS_PROPERTY'}]}],
            'properties': ['meeting_sourced_by'], 'limit': 100}
        if after:
            body['after'] = after
        r = requests.post('https://api.hubapi.com/crm/v3/objects/meetings/search',
                          headers=HS, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        for m in d.get('results', []):
            yield m
        after = (d.get('paging') or {}).get('next', {}).get('after')
        if not after:
            return


def contacts_for_meeting(mid):
    r = requests.get(
        f'https://api.hubapi.com/crm/v4/objects/meetings/{mid}/associations/contacts',
        headers=HS, timeout=15)
    return [str(a['toObjectId']) for a in r.json().get('results', [])] if r.status_code == 200 else []


def contact_sdr_owner(cid):
    r = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                     headers=HS, params={'properties': 'sdr_owner,email'}, timeout=15)
    return (r.json().get('properties') or {}) if r.status_code == 200 else {}


def main():
    counts = {'seen_meetings': 0, 'would_set': 0, 'set': 0, 'skipped_filled': 0, 'no_bdr': 0}
    for m in search_sourced_meetings():
        counts['seen_meetings'] += 1
        owner = m['properties'].get('meeting_sourced_by')
        val = sheet_sync.bdr_sdr_owner_value(owner)
        if not val:
            counts['no_bdr'] += 1
            continue
        for cid in contacts_for_meeting(m['id']):
            props = contact_sdr_owner(cid)
            if (props.get('email') or '').lower().endswith(('@furtherai.com', '@further.ai')):
                continue  # skip internal teammates
            if props.get('sdr_owner'):
                counts['skipped_filled'] += 1
                continue
            if APPLY:
                requests.patch(f'https://api.hubapi.com/crm/v3/objects/contacts/{cid}',
                               headers=HS, json={'properties': {'sdr_owner': val}}, timeout=15)
                counts['set'] += 1
            else:
                counts['would_set'] += 1
                print(f'[dry] contact={cid} <- sdr_owner={val}')
    print('[backfill] done', counts)


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Dry-run and sanity-check the volume.**

Run: `cd ~/meetingbuddy && HS_API_KEY=$HS_API_KEY python3 -W ignore scripts/backfill_sdr_owner.py 2>&1 | tail -5`
Expected: a `[backfill] done {...}` summary with `would_set` > 0 and `set` == 0 (dry-run made no writes). Eyeball a few `[dry]` lines for correct names.

- [ ] **Step 3: Apply.**

Run: `cd ~/meetingbuddy && HS_API_KEY=$HS_API_KEY python3 -W ignore scripts/backfill_sdr_owner.py --apply 2>&1 | tail -3`
Expected: `set` matches the prior `would_set`, `skipped_filled` accounts for already-attributed contacts.

- [ ] **Step 4: Verify a sample.** Re-run the dry-run; `would_set` should now be ~0 (everything filled). Spot-check 2–3 contacts in HubSpot show the expected BDR.

- [ ] **Step 5: Commit**

```bash
cd ~/meetingbuddy
git add scripts/backfill_sdr_owner.py
git commit -m "feat(sdr): one-time backfill of contact sdr_owner from meeting_sourced_by"
```

---

### Task 5: Deal-level `sdr_owner` propagation (decision-gated)

Per spec, on deal create the deal's `sdr_owner` should copy from the primary associated contact. Implement as ONE mechanism to avoid double-writes. Default recommendation: a **HubSpot workflow** (trigger: Deal created; action: copy associated contact `sdr_owner` → deal `sdr_owner`), because deal creation is AE-triggered and may not pass through bot code.

**Files:** none if workflow (portal config). If code path is chosen instead, modify the deal-creation site referenced by [[scheduled_deal_sync_disabled]].

- [ ] **Step 1: Confirm the choice with the owner** (workflow vs. code). Until confirmed, do not build both.
- [ ] **Step 2 (workflow path):** Settings → Workflows → Deal-based → trigger "Deal created" → action "Copy property value" from associated Contact `sdr_owner` to Deal `sdr_owner`, only if Deal `sdr_owner` is empty.
- [ ] **Step 3: Verify** by creating a test deal from a contact with a known `sdr_owner`; confirm the deal inherits it and AE owner (`hubspot_owner_id`) is unaffected.

---

### Task 6: [Human decision] Canonical-field reconciliation

The biggest data decision; not automatable. Decide the survivor among `sdr` (Contacts, owner-id, 18,166 rows, Z/J/D-only data), `sdr_owner` (Contacts+Deals, strings, 6,119), and the overlapping `meeting_sourced` / deals.`sourced_by`. Then migrate and deprecate.

- [ ] **Step 1:** Owner + admin pick the canonical field per object (plan default: `sdr_owner`).
- [ ] **Step 2:** Write a migration script (model on Task 4) to move data from the deprecated field(s) into the canonical one, fill-if-empty.
- [ ] **Step 3:** Dry-run, review counts, apply.
- [ ] **Step 4:** Hide/deprecate the losing fields in the portal (do not delete until a grace period passes — preserves the 18k `sdr` values as a fallback).

---

### Task 7: [Docs] Publish SOP #1 and SOP #4

The full SOP text is already in the spec. Publishing = moving it where BDRs/AEs/Ops will find it and linking from onboarding.

- [ ] **Step 1:** Copy SOP #1 (Meeting Logging & Opp Creation) and SOP #4 (Upload De-dup) from the spec into the team's canonical docs location (Notion/Drive — confirm with owner).
- [ ] **Step 2:** Link both from BDR/AE onboarding.
- [ ] **Step 3:** Announce in `#bdr-team`.

---

### Task 8: [Process] `qualified_stage` adoption

`qualified_stage` exists but only 8 meetings use it. The work is adoption, not config.

- [ ] **Step 1:** Confirm `qualified_stage` is on the AE meeting-edit card (portal).
- [ ] **Step 2:** Agree the qualified definition (`s0`/`s1`/`won` = qualified) per SOP #1.
- [ ] **Step 3:** AEs set it on meetings going forward; revisit usage counts in 2 weeks (re-run the count from the spec's validation).

---

## Self-Review

- **Spec coverage:** #2 population → Tasks 1–5; canonical reconciliation → Task 6; #1/#4 SOPs → Task 7; #3 → Task 8. All spec sections map to a task.
- **Placeholder scan:** none — all code steps contain full code; manual/admin tasks have exact portal paths and verify commands. `CONTACT_ID` in Task 3 Step 5 is an intentional manual substitution, called out as such.
- **Type consistency:** `bdr_sdr_owner_value(owner_id) -> str` used identically in Tasks 2, 3, and 4; `hs_set_contact_sdr_owner(contact_id, owner_id)` defined in Task 3 and not referenced elsewhere; `sdr_owner` property name consistent throughout.
