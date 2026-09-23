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

def test_prospect_organized_with_bdr_cc_counts():
    # PROSPECT (external) organized + BDR merely cc'd -> the AE's meeting -> counts.
    # (Policy: a prospect-sent invite is AE-earned even if a BDR is on the thread.)
    org = {"self": False, "email": "cfo@acme.com"}
    e = ev(summary="ITC // Acme", organizer=org,
           attendees=[{"email": "cfo@acme.com"}, {"email": _BDR}])
    assert bc.count_bookings([e], AE, START, END) == 1

def test_furtherai_bdr_organizer_excluded():
    # A FurtherAI BDR is the organizer -> BDR-sourced -> excluded.
    org = {"self": False, "email": _BDR}
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
    org_bdr = {"self": False, "email": _BDR}
    # External (prospect) organizer + BDR cc'd -> NOT bdr-booked (the AE's meeting)
    assert not bc.has_bdr_attendee(
        {"organizer": org_ext, "attendees": [{"email": _BDR}, {"email": "cfo@acme.com"}]}, AE)
    # FurtherAI BDR is the organizer -> bdr-booked
    assert bc.has_bdr_attendee(
        {"organizer": org_bdr, "attendees": [{"email": _BDR}, {"email": "cfo@acme.com"}]}, AE)
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


# --- dedup: only one board survives a restart ------------------------------
def test_is_board_message_matches_only_boards(monkeypatch):
    monkeypatch.setattr(bc, "BLITZ_TITLE", "AE Blitz: Booked Meetings")
    me = "UBOT"
    board = {"user": me, "text": "*AE Blitz: Booked Meetings*\n```\n...\n🔥 0 booked as a team\n```"}
    hype  = {"user": me, "text": "Nia is ON FIRE 🔥"}
    other = {"user": "UHUMAN", "text": "🔥 0 booked as a team"}
    assert bc._is_board_message(board, me) is True
    assert bc._is_board_message(hype, me) is False   # hype isn't a board
    assert bc._is_board_message(other, me) is False  # not the bot


class _SweepClient:
    def __init__(self, msgs):
        self._msgs = msgs
        self.deleted = []
    def auth_test(self):
        return {"user_id": "UBOT"}
    def conversations_history(self, channel=None, limit=None):
        return {"messages": self._msgs}
    def chat_delete(self, channel=None, ts=None):
        self.deleted.append(ts)
        return {"ok": True}


def test_delete_prior_boards_removes_all_boards(monkeypatch):
    monkeypatch.setattr(bc, "BLITZ_TITLE", "AE Blitz: Booked Meetings")
    monkeypatch.setattr(bc, "BLITZ_CHANNEL_ID", "C_TEST")
    msgs = [
        {"user": "UBOT", "ts": "1", "text": "*AE Blitz: Booked Meetings*\n🔥 0 booked as a team"},
        {"user": "UBOT", "ts": "2", "text": "Nia is ON FIRE 🔥"},          # hype, keep
        {"user": "UBOT", "ts": "3", "text": "*AE Blitz: Booked Meetings*\n🔥 1 booked as a team"},
        {"user": "UHUMAN", "ts": "4", "text": "gm team"},                   # human, keep
    ]
    c = _SweepClient(msgs)
    n = bc.delete_prior_boards(c)
    assert n == 2
    assert c.deleted == ["1", "3"]  # both boards gone, hype + human untouched


# --- tie handling: tied counts share a medal -------------------------------
from leaderboard import render_text, _rank_index  # noqa: E402

def test_rank_index_ties_share_rank():
    assert _rank_index(1, [1, 1]) == 0        # tied at top -> both rank 0 (gold)
    assert _rank_index(3, [3, 1, 1]) == 0
    assert _rank_index(1, [3, 1, 1]) == 1     # tied for 2nd -> both silver
    assert _rank_index(1, [2, 2, 1]) == 2     # two golds above -> bronze

def test_render_text_tied_leaders_both_gold():
    txt = render_text([("Nia", 1), ("Nick Margay", 1)], "AE Blitz", 2)
    gold = "\U0001F947"; silver = "\U0001F948"
    rows = [l for l in txt.splitlines() if ("Nia" in l or "Nick Margay" in l)]
    assert len(rows) == 2
    assert all(gold in l for l in rows)       # both gold
    assert silver not in txt                  # no silver when tied at top

def test_render_text_gold_then_bronze_when_two_tied_first():
    # [2,2,1] -> two golds, then bronze (silver skipped, standard competition ranking)
    txt = render_text([("A", 2), ("B", 2), ("C", 1)], "AE Blitz", 5)
    gold = "\U0001F947"; bronze = "\U0001F949"
    assert txt.count(gold) == 2
    assert bronze in txt
