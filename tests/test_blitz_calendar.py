import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blitz_calendar as bc  # noqa: E402

TZ = datetime.timezone.utc
START = datetime.datetime(2026, 9, 18, 8, 0, tzinfo=TZ)
END = datetime.datetime(2026, 9, 18, 20, 0, tzinfo=TZ)
IN = "2026-09-18T12:00:00Z"      # inside window
OUT = "2026-09-17T12:00:00Z"     # before window
AE = "nia@furtherai.com"


def ev(created=IN, status="confirmed", summary="ITC // Acme + FurtherAI",
       organizer=None, attendees=None, eid="e1", rec=None):
    if organizer is None:
        organizer = {"self": True, "email": AE}
    if attendees is None:
        attendees = [{"email": "cfo@acme-insurance.com"}]
    e = {"created": created, "status": status, "summary": summary,
         "organizer": organizer, "attendees": attendees, "id": eid}
    if rec:
        e["recurringEventId"] = rec
    return e


def c(events):
    return bc.count_bookings(events, AE, START, END)


def test_basic_organizer_external():
    assert c([ev()]) == 1

def test_out_of_window():
    assert c([ev(created=OUT)]) == 0

def test_all_internal_guests():
    assert c([ev(attendees=[{"email": "teammate@furtherai.com"}])]) == 0

def test_freemail_guest_excluded():
    assert c([ev(attendees=[{"email": "someone@gmail.com"}])]) == 0

def test_no_attendees():
    assert c([ev(attendees=[])]) == 0

def test_resource_guest_excluded():
    assert c([ev(attendees=[{"email": "room@resource.calendar.google.com", "resource": True}])]) == 0

def test_cancelled_status():
    assert c([ev(status="cancelled")]) == 0

def test_cancelled_title_prefix():
    assert c([ev(summary="Canceled: Acme + FurtherAI")]) == 0
    assert c([ev(summary="Cancelled: Acme + FurtherAI")]) == 0

def test_teammate_organized_attendee_only_not_credited():
    org = {"self": False, "email": "otherbdr@furtherai.com"}
    assert c([ev(organizer=org)]) == 0

def test_exec_organized_credits_ae():
    org = {"self": False, "email": "assistant@acme-insurance.com"}
    assert c([ev(organizer=org)]) == 1

def test_recurring_series_collapsed():
    a = ev(eid="i1", rec="series1")
    b = ev(eid="i2", rec="series1")
    d = ev(eid="i3", rec="series1")
    assert c([a, b, d]) == 1

def test_dedup_by_id():
    assert c([ev(eid="x"), ev(eid="x")]) == 1

def test_two_distinct_meetings():
    assert c([ev(eid="a", summary="ITC // Acme"), ev(eid="b", summary="ITC // Beta Co",
              attendees=[{"email": "vp@beta-co.com"}])]) == 2

def test_external_guests_helper():
    e = ev(attendees=[{"email": "a@furtherai.com"}, {"email": "b@gmail.com"},
                      {"email": "c@realco.com"}])
    assert bc.external_guests(e) == ["c@realco.com"]

def test_parse_iso():
    assert bc.parse_iso("2026-09-18T12:00:00Z") == START.replace(hour=12)
    assert bc.parse_iso(None) is None
    assert bc.parse_iso("garbage") is None

def test_display_name():
    assert bc.display_name("nia@furtherai.com") == "Nia"
    assert bc.display_name("jon.doe@furtherai.com") == "Jon Doe"

def test_itc_title_required():
    assert c([ev(summary="FurtherAI // Acme demo")]) == 0   # no ITC in title
    assert c([ev(summary="ITC // Acme + FurtherAI")]) == 1  # has ITC
    assert c([ev(summary="itc exec meeting @ Summit")]) == 1  # case-insensitive
    assert c([ev(summary="FurtherAI ITC Vegas exec")]) == 1   # ITC anywhere in title

def test_bdr_domain_excluded():
    # BDR posted acme.com in #conference-meetings — AE's ITC meeting with acme.com is skipped
    bdr_doms = {"acme-insurance.com"}
    e = ev(summary="ITC // Acme exec meeting")
    assert bc.count_bookings([e], AE, START, END, bdr_domains=bdr_doms) == 0

def test_non_bdr_domain_counts():
    # BDR posted acme.com but this meeting is with beta.com — AE gets credit
    bdr_doms = {"acme-insurance.com"}
    e = ev(summary="ITC // Beta Insurance", attendees=[{"email": "cfo@beta.com"}])
    assert bc.count_bookings([e], AE, START, END, bdr_domains=bdr_doms) == 1

def test_mixed_domains_counts():
    # Meeting has acme.com (BDR) + beta.com (AE own contact) — not all BDR, counts
    bdr_doms = {"acme-insurance.com"}
    e = ev(summary="ITC // Multi", attendees=[{"email": "cfo@acme-insurance.com"}, {"email": "vp@beta.com"}])
    assert bc.count_bookings([e], AE, START, END, bdr_domains=bdr_doms) == 1
