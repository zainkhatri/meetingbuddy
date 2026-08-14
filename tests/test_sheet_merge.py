"""Tests for sheet_sync._merge_existing_row — the update-merge policy.

Human-curated columns are fill-empty-only (never overwritten). Bot-derived
columns (Conference, Meeting Status) are re-derived from HubSpot each reconcile,
so a non-empty new value overwrites the old one. This is what lets a re-stamped
conference (e.g. 'other' -> 'RIMS RiskWorld') actually reach Ellen's sheet.
"""
import sheet_sync as s

HEADERS = ["Conference", "Meeting Sourced By", "Account Executive",
           "FurtherAI Rep in Meeting", "Meeting Date", "Meeting Time",
           "Meeting Location", "Prospect Company", "Prospect Name",
           "Prospect Title", "Prospect Email", "Meeting Status", "Notes",
           "Follow-Up Demo Scheduled?"]
LAST = len(HEADERS)


def _row(**cells):
    r = [""] * len(HEADERS)
    for k, v in cells.items():
        r[HEADERS.index(k.replace("_", " "))] = v
    return r


def test_bot_owned_conference_overwrites_when_changed():
    current = _row(Conference="Other", Prospect_Company="Unico")
    payload = {"Conference": "RIMS RiskWorld", "Prospect Company": "Unico"}
    merged, changed = s._merge_existing_row(HEADERS, current, payload, LAST)
    assert merged[0] == "RIMS RiskWorld"
    assert changed is True


def test_human_column_never_overwritten():
    current = _row(Notes="spoke to Eric", Conference="Other")
    payload = {"Notes": "bot note", "Conference": "Other"}
    merged, changed = s._merge_existing_row(HEADERS, current, payload, LAST)
    assert merged[HEADERS.index("Notes")] == "spoke to Eric"   # human wins
    assert changed is False   # Conference unchanged, Notes preserved


def test_bot_owned_empty_payload_keeps_old():
    current = _row(Conference="WSIA UW Summit 2026")
    payload = {"Conference": ""}   # reconcile had nothing -> must not wipe
    merged, changed = s._merge_existing_row(HEADERS, current, payload, LAST)
    assert merged[0] == "WSIA UW Summit 2026"
    assert changed is False


def test_fill_empty_human_column_still_works():
    current = _row(Conference="WSIA UW Summit 2026")   # Prospect Email empty
    payload = {"Conference": "WSIA UW Summit 2026", "Prospect Email": "eric@unico.com"}
    merged, changed = s._merge_existing_row(HEADERS, current, payload, LAST)
    assert merged[HEADERS.index("Prospect Email")] == "eric@unico.com"
    assert changed is True


def test_bot_owned_equal_value_is_noop():
    current = _row(Conference="WSIA UW Summit 2026")
    payload = {"Conference": "WSIA UW Summit 2026"}
    merged, changed = s._merge_existing_row(HEADERS, current, payload, LAST)
    assert merged[0] == "WSIA UW Summit 2026"
    assert changed is False
