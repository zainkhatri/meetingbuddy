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

def test_bdr_exact_email_excluded():
    # BDR posted cfo@acme.com — AE meeting with that exact person is skipped
    bdr_contacts = {"cfo@acme-insurance.com"}
    e = ev(summary="ITC // Acme exec meeting")
    assert bc.count_bookings([e], AE, START, END, bdr_contacts=bdr_contacts) == 0

def test_bdr_different_person_same_company_counts():
    # BDR posted cfo@acme.com but AE booked vp@acme.com — different person, counts
    bdr_contacts = {"cfo@acme-insurance.com"}
    e = ev(summary="ITC // Acme exec meeting", attendees=[{"email": "vp@acme-insurance.com"}])
    assert bc.count_bookings([e], AE, START, END, bdr_contacts=bdr_contacts) == 1

def test_bdr_non_contact_counts():
    # BDR posted someone at acme, AE booked someone at beta — unrelated, counts
    bdr_contacts = {"cfo@acme-insurance.com"}
    e = ev(summary="ITC // Beta Insurance", attendees=[{"email": "cfo@beta.com"}])
    assert bc.count_bookings([e], AE, START, END, bdr_contacts=bdr_contacts) == 1

def test_mixed_contacts_counts():
    # Meeting has BDR-booked person + AE's own contact — not all BDR, counts
    bdr_contacts = {"cfo@acme-insurance.com"}
    e = ev(summary="ITC // Multi", attendees=[{"email": "cfo@acme-insurance.com"}, {"email": "vp@beta.com"}])
    assert bc.count_bookings([e], AE, START, END, bdr_contacts=bdr_contacts) == 1

# --- BDR attendee tests ---
_BDR = "jacob@furtherai.com"

def test_bdr_attendee_ae_not_organizer_excluded():
    # BDR is on invite, exec is organizer, AE is attendee -> BDR booked it -> skip
    org = {"self": False, "email": "cfo@acme.com"}
    e = ev(summary="ITC // Acme", organizer=org,
           attendees=[{"email": "cfo@acme.com"}, {"email": _BDR}])
    assert bc.count_bookings([e], AE, START, END) == 0

def test_bdr_attendee_ae_is_organizer_counts():
    # AE organized the meeting AND invited BDR for support -> AE's work -> count
    org = {"self": True, "email": AE}
    e = ev(summary="ITC // Acme", organizer=org,
           attendees=[{"email": "cfo@acme.com"}, {"email": _BDR}])
    assert bc.count_bookings([e], AE, START, END) == 1

def test_has_bdr_attendee_helper():
    org_ae = {"self": True, "email": AE}
    org_ext = {"self": False, "email": "cfo@acme.com"}
    # BDR on invite, external organizer
    assert bc.has_bdr_attendee(
        {"organizer": org_ext, "attendees": [{"email": _BDR}, {"email": "cfo@acme.com"}]}, AE)
    # BDR on invite, AE is organizer -> False (AE's meeting)
    assert not bc.has_bdr_attendee(
        {"organizer": org_ae, "attendees": [{"email": _BDR}, {"email": "cfo@acme.com"}]}, AE)
    # No BDR on invite -> False
    assert not bc.has_bdr_attendee(
        {"organizer": org_ext, "attendees": [{"email": "cfo@acme.com"}]}, AE)


# --- hardening: calendar read retry (#1) -----------------------------------
class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._p = payload or {}
        self.text = text
    def json(self):
        return self._p


def test_get_events_page_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise bc.requests.Timeout("read timeout")
        return _FakeResp(200, {"items": [1, 2]})
    monkeypatch.setattr(bc.requests, "get", fake_get)
    monkeypatch.setattr(bc.time, "sleep", lambda *_a, **_k: None)
    d = bc._get_events_page("tok", {}, attempts=3)
    assert d["items"] == [1, 2]
    assert calls["n"] == 3  # retried twice, succeeded on third


def test_get_events_page_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(bc.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(bc.requests.Timeout("x")))
    monkeypatch.setattr(bc.time, "sleep", lambda *_a, **_k: None)
    try:
        bc._get_events_page("tok", {}, attempts=2)
        assert False, "should have raised"
    except bc.requests.Timeout:
        pass


def test_get_events_page_non_retryable_raises(monkeypatch):
    monkeypatch.setattr(bc.requests, "get", lambda *a, **k: _FakeResp(404, text="nope"))
    monkeypatch.setattr(bc.time, "sleep", lambda *_a, **_k: None)
    try:
        bc._get_events_page("tok", {}, attempts=3)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "404" in str(e)


# --- hardening: hype persistence across restart (#2) ------------------------
class _FakeClient:
    token = "xoxb-test"
    def __init__(self):
        self.hype = []
    def chat_postMessage(self, channel=None, text=None, **k):
        self.hype.append(text)
        return {"ts": "1.0"}


def test_process_counts_fires_hype_on_increase(monkeypatch):
    monkeypatch.setattr(bc, "BLITZ_AES", [AE])
    c = _FakeClient()
    last = {}
    rows = bc.process_counts(c, {AE: 2}, last)
    assert rows == [("Nia", 2)]
    assert last[AE] == 2
    assert len(c.hype) == 2  # first + second booking each get a shout


def test_process_counts_no_refire_from_persisted_last(monkeypatch):
    # simulates a restart: `last` restored from disk == current counts -> silence
    monkeypatch.setattr(bc, "BLITZ_AES", [AE])
    c = _FakeClient()
    last = {AE: 2}
    rows = bc.process_counts(c, {AE: 2}, last)
    assert rows == [("Nia", 2)]
    assert c.hype == []  # no re-spam on restart


def test_process_counts_keeps_last_on_read_failure(monkeypatch):
    monkeypatch.setattr(bc, "BLITZ_AES", [AE])
    c = _FakeClient()
    last = {AE: 3}
    rows = bc.process_counts(c, {AE: None}, last)
    assert rows == [("Nia", 3)]  # keep last-known when the read failed
    assert c.hype == []
