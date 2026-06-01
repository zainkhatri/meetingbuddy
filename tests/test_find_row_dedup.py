"""Regression tests for sheet_sync._find_row dedup matching.

These pin the behavior that keeps Meeting Bot from appending duplicate rows to
Ellen's tracker. If you change _find_row and one of these fails, you are about
to reintroduce a duplicate-row bug — fix the code, not the test.
"""
import sheet_sync as s

HEADERS = ["Conference", "Meeting Sourced By", "Account Executive",
           "FurtherAI Rep in Meeting", "Meeting Date", "Meeting Time",
           "Meeting Location", "Prospect Company", "Prospect Name",
           "Prospect Title", "Prospect Email", "Meeting Status", "Notes",
           "Follow-Up Demo Scheduled?"]


def _row(co, dt, tm, nm):
    r = [""] * len(HEADERS)
    r[7], r[4], r[5], r[8] = co, dt, tm, nm
    return r


def _sheet():
    s._headers = {h: i + 1 for i, h in enumerate(HEADERS)}
    return [HEADERS,
            _row("Ironshore Insurance", "6/3", "2:10 PM ET", "John Fogarty"),   # 2
            _row("Aon", "6/4", "10:10 AM ET", "Tom Macari"),                    # 3
            _row("Aon", "6/4", "10:50 AM ET", "Tom Macari"),                    # 4
            _row("Marsh", "6/3", "9:10 am ET", "Srividya Santosh")]             # 5


def find(rows, co, dt, tm, nm):
    return s._find_row(rows, co, dt, contact_name=nm, meeting_time=tm)


def test_company_spelling_dupe_same_time_matches():
    # Same person+date, different company string -> must dedup, not append.
    assert find(_sheet(), "Liberty Mutual Insurance", "6/3", "2:10 PM ET", "John Fogarty") == 2


def test_company_spelling_dupe_blank_incoming_time_matches():
    assert find(_sheet(), "Liberty Mutual", "6/3", "", "John Fogarty") == 2


def test_exact_company_date_primary_match():
    assert find(_sheet(), "Ironshore Insurance", "6/3", "2:10 PM ET", "John Fogarty") == 2


def test_different_date_same_person_appends():
    assert find(_sheet(), "Liberty Mutual", "6/5", "2:10 PM ET", "John Fogarty") is None


def test_multirow_picks_correct_time_slot():
    rows = _sheet()
    assert find(rows, "Aon", "6/4", "10:50 AM ET", "Tom Macari") == 4
    assert find(rows, "Aon", "6/4", "10:10 AM ET", "Tom Macari") == 3


def test_multirow_new_time_slot_appends():
    # Same company/date/person but a genuinely new time -> distinct meeting.
    assert find(_sheet(), "Aon", "6/4", "9:00 AM ET", "Tom Macari") is None


def test_lone_match_is_time_agnostic():
    # A human reformatting the time cell must not cause a dup on the next sync.
    assert find(_sheet(), "Marsh", "6/3", "9:10 AM ET", "Srividya Santosh") == 5


def test_brand_new_meeting_appends():
    assert find(_sheet(), "NewCo", "7/1", "1:00 PM ET", "Jane New") is None
