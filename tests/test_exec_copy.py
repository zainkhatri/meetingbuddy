import requests
import vp_escalation as vp


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}
        self.ok = 200 <= code < 300

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


_SRC = {"summary": "C&F // FurtherAI", "organizer": {"email": "angelo@cfins.com"},
        "start": {"dateTime": "2026-11-10T10:00:00-08:00"},
        "end": {"dateTime": "2026-11-10T10:30:00-08:00"},
        "location": "Microsoft Teams Meeting", "description": "teams body",
        "conferenceData": {"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1"},
            {"entryPointType": "video", "uri": "https://teams.microsoft.com/l/abc"}]}}


def _fake(monkeypatch, gets, posts):
    monkeypatch.setattr(vp, "_gcal_token", lambda subject=None: "tok")
    monkeypatch.setattr(requests, "get", lambda url, **k: gets.pop(0))
    monkeypatch.setattr(requests, "post", lambda url, **k: posts.append((url, k)) or _Resp(200))


def test_join_link_prefers_video_entry_point():
    assert vp._join_link(_SRC) == "https://teams.microsoft.com/l/abc"
    assert vp._join_link({"hangoutLink": "https://meet.google.com/x"}) == "https://meet.google.com/x"
    assert vp._join_link({"location": "Room 4"}) == ""


def test_copy_id_is_deterministic_and_google_safe():
    a, b = vp.exec_copy_id("mtg1"), vp.exec_copy_id("mtg1")
    assert a == b and a != vp.exec_copy_id("mtg2")
    assert set(a) <= set("0123456789abcdefghijklmnopqrstuv")


def test_copy_creates_event_on_exec_calendar(monkeypatch):
    posts = []
    _fake(monkeypatch, [_Resp(404), _Resp(404)], posts)
    r = vp.copy_to_exec(_SRC, "aman", "mtg1")
    assert r["performed"] and r["exec"] == "aman"
    url, k = posts[0]
    assert "aman@" in url and k["params"]["sendUpdates"] == "none"
    assert k["json"]["location"] == "https://teams.microsoft.com/l/abc"
    assert k["json"]["start"] == _SRC["start"] and "attendees" not in k["json"]


def test_copy_skips_when_other_exec_already_has_it(monkeypatch):
    posts = []
    _fake(monkeypatch, [_Resp(404), _Resp(200, {"status": "confirmed"})], posts)
    r = vp.copy_to_exec(_SRC, "aman", "mtg1")
    assert r == {"performed": False, "reason": "already_present", "exec": "zac"}
    assert posts == []


def test_add_guest_hands_back_client_organized(monkeypatch):
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    monkeypatch.setattr(vp, "_gcal_token", lambda subject=None: "tok")
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(200, _SRC))
    monkeypatch.setattr(requests, "patch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no patch")))
    r = vp.add_guest("evt", "aman", calendar_id="mike@furtherai.com")
    assert r["reason"] == "client_organized" and r["event"]["summary"] == _SRC["summary"]


def test_aman_alias_counts_as_present(monkeypatch):
    monkeypatch.setattr(vp, "_gcal_token", lambda subject=None: "tok")
    ev = {"organizer": {"email": "nia@furtherai.com"}, "attendees": [{"email": "AG@furtherai.com"}]}
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(200, ev))
    assert vp.event_has_exec("nia@furtherai.com", "evt") is True
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    assert vp.add_guest("evt", "aman", calendar_id="nia@furtherai.com")["reason"] == "already_present"


def test_alias_does_not_match_lookalike(monkeypatch):
    monkeypatch.setattr(vp, "_gcal_token", lambda subject=None: "tok")
    ev = {"attendees": [{"email": "rag@furtherai.com"}, {"email": "zachary@x.com"}]}
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(200, ev))
    assert vp.event_has_exec("nia@furtherai.com", "evt") is False
