"""Tests for invite_offer: the Send/Skip calendar-invite offer on booking posts.

Safety-critical properties: never offer when an invite already exists or the
data is incomplete/past; only the poster or an admin may send; the event body
puts the poster as organizer with ONLY prospect + AE as guests; a double click
never creates two events.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invite_offer as io  # noqa: E402

NOW = datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)


def _offer(**over):
    o = {"organizer": "zain@furtherai.com", "poster_slack": "U_POSTER",
         "prospect_name": "Tanya Unsworth", "prospect_email": "tunsworth@hanover.com",
         "company": "The Hanover Insurance Group", "start_utc": "2026-09-30T19:00:00Z",
         "duration_min": 15, "is_conference": True, "conf_short": "ITC",
         "location": "FurtherAI Booth #1619", "ae_email": "nick@furtherai.com",
         "ae_name": "Nick Margay"}
    o.update(over)
    return o


# ── decide_offer ─────────────────────────────────────────────────────────────
def test_offer_when_everything_present_and_no_invite():
    assert io.decide_offer(_offer(), invite_exists=False, now=NOW) == (True, "ok")


def test_no_offer_when_invite_exists():
    assert io.decide_offer(_offer(), invite_exists=True, now=NOW) == (False, "invite_exists")


def test_no_offer_when_calendar_check_failed():
    # None = couldn't check; never risk a duplicate invite
    assert io.decide_offer(_offer(), invite_exists=None, now=NOW) == (False, "calendar_unknown")


def test_no_offer_without_email():
    assert io.decide_offer(_offer(prospect_email=""), invite_exists=False, now=NOW) == (False, "no_email")


def test_no_offer_without_time():
    assert io.decide_offer(_offer(start_utc=""), invite_exists=False, now=NOW) == (False, "no_time")


def test_no_offer_for_past_meeting():
    assert io.decide_offer(_offer(start_utc="2026-09-27T19:00:00Z"), invite_exists=False, now=NOW) == (False, "past")


def test_no_offer_without_organizer():
    assert io.decide_offer(_offer(organizer=""), invite_exists=False, now=NOW) == (False, "no_organizer")


def test_no_offer_for_internal_email():
    assert io.decide_offer(_offer(prospect_email="nick@furtherai.com"), invite_exists=False, now=NOW) == (False, "internal_email")


# ── event_title / build_event ────────────────────────────────────────────────
def test_conference_title():
    assert io.event_title(_offer()) == "FurtherAI + The Hanover Insurance Group (ITC)"


def test_demo_title():
    assert io.event_title(_offer(is_conference=False, conf_short="")) == "FurtherAI + The Hanover Insurance Group"


def test_conference_event_guests_are_prospect_and_ae_only():
    ev = io.build_event(_offer())
    emails = [a["email"] for a in ev["attendees"]]
    assert emails == ["tunsworth@hanover.com", "nick@furtherai.com"]
    assert "zain@furtherai.com" not in emails          # organizer is not a guest
    assert ev["location"] == "FurtherAI Booth #1619"
    assert "conferenceData" not in ev                  # in person: no Meet link
    assert ev["start"]["dateTime"] == "2026-09-30T19:00:00Z"
    assert ev["end"]["dateTime"] == "2026-09-30T19:15:00Z"


def test_demo_event_gets_meet_link_and_no_location():
    ev = io.build_event(_offer(is_conference=False, conf_short="", location="", duration_min=30))
    assert ev["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert "location" not in ev
    assert ev["end"]["dateTime"] == "2026-09-30T19:30:00Z"


def test_no_ae_means_prospect_only():
    ev = io.build_event(_offer(ae_email="", ae_name=""))
    assert [a["email"] for a in ev["attendees"]] == ["tunsworth@hanover.com"]


def test_ae_who_is_organizer_not_duplicated():
    ev = io.build_event(_offer(ae_email="zain@furtherai.com"))
    assert [a["email"] for a in ev["attendees"]] == ["tunsworth@hanover.com"]


def test_conference_default_location():
    ev = io.build_event(_offer(location=""))
    assert ev["location"] == "FurtherAI Booth"


# ── payload round trip + auth ────────────────────────────────────────────────
def test_payload_round_trip_fits_slack_limit():
    raw = io.encode_payload(_offer())
    assert len(raw) <= 2000
    assert io.decode_payload(raw) == _offer()


def test_decode_rejects_garbage():
    assert io.decode_payload("not json") is None
    assert io.decode_payload(json.dumps({"organizer": "x"})) is None


def test_only_poster_or_admin_may_click():
    assert io.may_click("U_POSTER", _offer(), admins={"U_ZAIN"}) is True
    assert io.may_click("U_ZAIN", _offer(), admins={"U_ZAIN"}) is True
    assert io.may_click("U_RANDO", _offer(), admins={"U_ZAIN"}) is False
    assert io.may_click("", _offer(), admins={"U_ZAIN"}) is False


# ── preview blocks ───────────────────────────────────────────────────────────
def test_preview_has_send_and_skip_buttons_with_payload():
    blocks = io.preview_blocks(_offer())
    actions = [b for b in blocks if b["type"] == "actions"][0]["elements"]
    ids = [e["action_id"] for e in actions]
    assert ids == ["invite_send", "invite_skip"]
    assert io.decode_payload(actions[0]["value"]) == _offer()
    text = json.dumps(blocks)
    assert "tunsworth@hanover.com" in text and "Nick Margay" in text
    assert "Wed Sep 30, 12:00 PM PT" in text


def test_preview_flags_missing_ae():
    text = json.dumps(io.preview_blocks(_offer(ae_email="", ae_name="")))
    assert "No AE owner in HubSpot" in text


def test_pt_formatting_handles_dst():
    assert io.fmt_pt("2026-09-30T19:00:00Z") == "Wed Sep 30, 12:00 PM PT"
    assert io.fmt_pt("2026-12-02T20:30:00Z") == "Wed Dec 2, 12:30 PM PT"


# ── invite_exists_in (pure matcher over fetched events) ──────────────────────
def _ex(evs, email="tunsworth@hanover.com", company="The Hanover Insurance Group", name="Tanya Unsworth"):
    return io.invite_exists_in(evs, email, company, name)


def test_existing_event_matched_by_attendee_email():
    assert _ex([{"summary": "Coffee", "attendees": [{"email": "TUnsworth@hanover.com"}]}]) is True


def test_placeholder_with_company_in_title_counts():
    # teammate's placeholder, no guests (the ePremium pattern)
    assert _ex([{"summary": "ITC chat - FurtherAI // Hanover", "attendees": [{"email": "ben@furtherai.com"}]}]) is True


def test_itc_app_invite_matched_by_prospect_name():
    # grip invites carry no prospect email, just the name in the title
    ev = {"summary": "Meeting with Tanya Unsworth (THG) at 1619",
          "attendees": [{"email": "calendar+1@mg.gripcontact.com"}]}
    assert _ex([ev]) is True


def test_name_in_guest_display_name_counts():
    ev = {"summary": "ITC", "attendees": [{"email": "t@gmail.com", "displayName": "Tanya Unsworth"}]}
    assert _ex([ev]) is True


def test_other_contact_same_company_does_not_block():
    # 3 Cincinnati contacts: Yuchen's invite must not suppress Bob's offer
    ev = {"summary": "FurtherAI + Cincinnati (ITC)", "attendees": [{"email": "yuchen_wang@cinfin.com"}]}
    assert io.invite_exists_in([ev], "robert_weishaar@cinfin.com", "The Cincinnati Insurance Companies",
                               "Bob Weishaar") is False


def test_unrelated_and_cancelled_not_matched():
    evs = [{"summary": "Lunch", "attendees": [{"email": "a@b.com"}]},
           {"summary": "FurtherAI + Hanover", "status": "cancelled", "attendees": []}]
    assert _ex(evs) is False


def test_short_last_name_ignored():
    # 'Li' would match everything; name rule needs >= 3 chars
    ev = {"summary": "Lisbon offsite", "attendees": []}
    assert io.invite_exists_in([ev], "joey@x.com", "Zzq Corp", "Joey Li") is False


def test_decide_skips_imminent_meeting():
    # on-the-spot booking starting in 20 min: they already have it handled
    assert io.decide_offer(_offer(start_utc="2026-09-28T20:20:00Z"), invite_exists=False, now=NOW) == (False, "imminent")


def test_team_calendars_dedup_and_internal_only():
    cals = io.team_calendars("Zain@furtherai.com", "nick@furtherai.com",
                             ["nick@furtherai.com", "fabio@furtherai.com", "x@gmail.com", ""])
    assert cals[:2] == ["zain@furtherai.com", "nick@furtherai.com"]
    assert "fabio@furtherai.com" in cals and "x@gmail.com" not in cals
    assert len(cals) == len(set(cals))


# ── send: double click creates one event ─────────────────────────────────────
def test_send_twice_creates_one_event():
    created = []
    state = {"events": []}

    def check(_offer):
        return io.invite_exists_in(state["events"], _offer["prospect_email"], _offer["company"],
                                   _offer["prospect_name"])

    def insert(_org, body, _conf):
        created.append(body)
        state["events"].append({"summary": body["summary"], "attendees": body["attendees"]})
        return {"id": f"e{len(created)}", "htmlLink": "https://cal/e"}

    first = io.send_invite(_offer(), check_exists=check, insert_event=insert)
    second = io.send_invite(_offer(), check_exists=check, insert_event=insert)
    assert first["status"] == "sent"
    assert second["status"] == "already_exists"
    assert len(created) == 1


def test_send_reports_calendar_failure():
    def check(*_a):
        raise RuntimeError("boom")
    out = io.send_invite(_offer(), check_exists=check, insert_event=lambda *a: None)
    assert out["status"] == "error"


def test_send_refuses_when_team_check_unknown():
    out = io.send_invite(_offer(), check_exists=lambda _o: None, insert_event=lambda *a: {"id": "x"})
    assert out["status"] == "error"


# ── offer_from_booking (parsed Slack post -> offer) ──────────────────────────
def _parsed(**over):
    p = {"contact_first_name": "Tanya", "contact_last_name": "Unsworth",
         "contact_email": "tunsworth@hanover.com", "company_name": "The Hanover Insurance Group",
         "meeting_date": "2026-09-30", "meeting_time_utc": "19:00",
         "conference_source": "itc_2026", "location": "Booth 1619"}
    p.update(over)
    return p


def test_offer_from_conference_booking():
    o = io.offer_from_booking(_parsed(), organizer="Zain@FurtherAI.com", poster_slack="U1",
                              ae_email="nick@furtherai.com", ae_name="Nick Margay",
                              is_conference=True, duration_min=15, conf_label="ITC Vegas 2026")
    assert o["start_utc"] == "2026-09-30T19:00:00Z"
    assert o["organizer"] == "zain@furtherai.com"
    assert o["conf_short"] == "ITC"
    assert o["location"] == "Booth 1619"
    assert o["prospect_name"] == "Tanya Unsworth"
    assert set(o) == set(io._PAYLOAD_KEYS)


def test_offer_without_time_has_empty_start():
    o = io.offer_from_booking(_parsed(meeting_time_utc=None), organizer="z@furtherai.com",
                              poster_slack="U1", ae_email="", ae_name="", is_conference=True,
                              duration_min=15, conf_label="")
    assert o["start_utc"] == ""


def test_unknown_conference_uses_label():
    o = io.offer_from_booking(_parsed(conference_source="future_x"), organizer="z@furtherai.com",
                              poster_slack="U1", ae_email="", ae_name="", is_conference=True,
                              duration_min=15, conf_label="Future X 2026")
    assert o["conf_short"] == "Future X 2026"


def test_demo_booking_has_no_conf_tag_or_location():
    o = io.offer_from_booking(_parsed(conference_source=None, location="Zoom"), organizer="z@furtherai.com",
                              poster_slack="U1", ae_email="", ae_name="", is_conference=False,
                              duration_min=30, conf_label="")
    assert o["conf_short"] == "" and o["location"] == "" and o["is_conference"] is False


def test_roster_read_failure_is_best_effort(monkeypatch):
    calls = []

    def fetch(cal, lo, hi):
        calls.append(cal)
        if cal == "gone@furtherai.com":
            return None   # permanent: user/calendar doesn't exist
        return [{"summary": "Meeting with Tanya Unsworth", "attendees": []}] if cal == "fabio@furtherai.com" else []
    monkeypatch.setattr(io, "gcal_fetch_events", fetch)
    cals = ["zain@furtherai.com", "nick@furtherai.com", "gone@furtherai.com", "fabio@furtherai.com"]
    assert io.invite_exists(cals, "tunsworth@hanover.com", "Hanover", "2026-09-30T19:00:00Z",
                            "Tanya Unsworth", required=2) is True
    assert "fabio@furtherai.com" in calls


def test_required_calendar_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(io, "gcal_fetch_events", lambda *a: None)
    assert io.invite_exists(["zain@furtherai.com"], "t@h.com", "H", "2026-09-30T19:00:00Z",
                            required=1) is None


# ── regressions from the real ITC replay (2026-09-28) ────────────────────────
def test_last_name_in_guest_email_local_part():
    # "Bryan / Fabio - ITC Booth 1619" with bseiter@acuity.com (sheet had no email)
    ev = {"summary": "Bryan / Fabio - ITC Booth 1619", "attendees": [{"email": "bseiter@acuity.com"}]}
    assert io.invite_exists_in([ev], "", "Acuity Insurance", "Bryan Seiter") is True
    ev2 = {"summary": "RGA x FurtherAI", "attendees": [{"email": "jen.jennings@rgare.com"}]}
    assert io.invite_exists_in([ev2], "", "Reinsurance Group of America", "Jennifer Jenning") is True


def test_company_core_skips_short_leading_words():
    assert io._company_core("JS Johnson") == "johnson"
    assert io._company_core("A-Max Insurance") == "max"


def test_internal_guest_email_never_name_matches():
    # a teammate named Smith must not count as the prospect
    ev = {"summary": "Standup", "attendees": [{"email": "jsmith@furtherai.com"}]}
    assert io.invite_exists_in([ev], "", "Zzq", "Keisha Smith") is False


def test_fetch_retries_once_on_timeout(monkeypatch):
    import requests as rq
    n = {"calls": 0}

    class R:
        status_code = 200
        def json(self): return {"items": [{"summary": "x"}]}

    def get(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise rq.Timeout("slow")
        return R()
    monkeypatch.setattr(io.vp, "_gcal_token", lambda subject=None: "tok")
    io._TOKENS.clear()
    monkeypatch.setattr(io._HTTP, "get", get)
    assert io.gcal_fetch_events("zain@furtherai.com", "a", "b") == [{"summary": "x"}]
    assert n["calls"] == 2


def test_roster_is_every_internal_hubspot_owner():
    class R:
        ok = True
        def __init__(self, j): self._j = j
        def json(self): return self._j
    pages = [{"results": [{"id": "1", "email": "Zac@furtherai.com"},
                          {"id": "2", "email": "unassigned@furtherai.com"},
                          {"id": "3", "email": "vendor@gmail.com"},
                          {"id": "4", "email": "nick@furtherai.com", "archived": True}],
              "paging": {"next": {"after": "x"}}},
             {"results": [{"id": "5", "email": "livvie@furtherai.com"}]}]
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        return R(pages[len(calls) - 1])
    out = io.team_roster_from_hubspot(api_key="k", http_get=get)
    assert out == ["zac@furtherai.com", "livvie@furtherai.com"]
    assert len(calls) == 2


def test_transient_roster_failure_is_unknown(monkeypatch):
    def fetch(cal, lo, hi):
        if cal == "jacob@furtherai.com":
            raise RuntimeError("TransportError")   # blip: could hide a teammate's invite
        return []
    monkeypatch.setattr(io, "gcal_fetch_events", fetch)
    cals = ["zain@furtherai.com", "nick@furtherai.com", "jacob@furtherai.com"]
    assert io.invite_exists(cals, "t@h.com", "H", "2026-09-30T19:00:00Z", required=2) is None


def test_fetch_permanent_vs_transient(monkeypatch):
    import requests as rq

    class R:
        def __init__(self, c): self.status_code, self.text = c, ""
        def json(self): return {}
    monkeypatch.setattr(io.vp, "_gcal_token", lambda subject=None: "tok")
    io._TOKENS.clear()
    monkeypatch.setattr(io._HTTP, "get", lambda *a, **k: R(404))
    assert io.gcal_fetch_events("gone@furtherai.com", "a", "b") is None
    monkeypatch.setattr(io._HTTP, "get", lambda *a, **k: R(503))
    try:
        io.gcal_fetch_events("zain@furtherai.com", "a", "b")
        assert False, "5xx must raise"
    except RuntimeError:
        pass


def test_invalid_grant_token_is_permanent(monkeypatch):
    def tok(subject=None):
        raise Exception("('invalid_grant: Invalid email or User ID', {})")
    monkeypatch.setattr(io.vp, "_gcal_token", tok)
    io._TOKENS.clear()
    assert io.gcal_fetch_events("dani@furtherai.com", "a", "b") is None


def test_token_cached_per_user(monkeypatch):
    n = {"mint": 0}

    def tok(subject=None):
        n["mint"] += 1
        return f"t-{subject}"
    monkeypatch.setattr(io.vp, "_gcal_token", tok)
    io._TOKENS.clear()
    assert io._token("a@furtherai.com") == "t-a@furtherai.com"
    assert io._token("a@furtherai.com") == "t-a@furtherai.com"
    assert io._token("b@furtherai.com") == "t-b@furtherai.com"
    assert n["mint"] == 2


def test_other_contacts_meeting_does_not_block_when_email_unknown():
    # MSIG 2026-09-28: Aika Kikuchi (no email) vs Nick's intro with Will Handley
    ev = {"summary": "Nick <> Will FurtherAI & MSIG Intro",
          "attendees": [{"email": "whandley@msigusa.com"}, {"email": "nick.n@furtherai.com"},
                        {"email": "c_188@resource.calendar.google.com"}]}
    assert io.invite_exists_in([ev], "", "MSIG", "Aika Kikuchi") is False


def test_itc_app_placeholder_still_blocks_on_company():
    ev = {"summary": "Meeting with A. K. (MSIG) at 1619",
          "attendees": [{"email": "calendar+1@mg.gripcontact.com"}, {"email": "ben@furtherai.com"}]}
    assert io.invite_exists_in([ev], "", "MSIG", "Aika Kikuchi") is True


def test_first_name_address_plus_company_title():
    ev = {"summary": "ITC - FurtherAI // Statement Insurance", "attendees": [{"email": "mark@statementinsurance.com"}]}
    assert io.invite_exists_in([ev], "", "Statement Insurance Agency", "Mark Hutchings") is True
    ev2 = {"summary": "FurtherAI // Meslee (ITC Connect)", "attendees": [{"email": "brett@meslee.com"}]}
    assert io.invite_exists_in([ev2], "", "Meslee Insurance Services", "Brett Tucker") is True


def test_first_name_without_company_title_does_not_match():
    ev = {"summary": "Coffee chat", "attendees": [{"email": "mark@othercorp.com"}]}
    assert io.invite_exists_in([ev], "", "Statement Insurance Agency", "Mark Hutchings") is False


def test_truncated_last_name_address():
    ev = {"summary": "ITC - FurtherAI + Nationwide", "attendees": [{"email": "mcqueb2@nationwide.com"}]}
    assert io.invite_exists_in([ev], "", "Nationwide", "Brandon McQueen") is True


def test_poster_allowed(monkeypatch):
    monkeypatch.delenv("INVITE_POSTERS", raising=False)
    assert io.poster_allowed("U1") is True
    monkeypatch.setenv("INVITE_POSTERS", " U2 , U3 ")
    assert io.poster_allowed("U3") is True and io.poster_allowed("U1") is False


# --- ask for the email when it's the only thing missing ---------------------

from datetime import datetime as _dt, timezone as _tz

_NOW = _dt(2099, 9, 1, tzinfo=_tz.utc)
_BASE = {"organizer": "matthew@furtherai.com", "prospect_email": "", "company": "Assurant",
         "prospect_name": "Noah Walsey", "start_utc": "2099-10-06T16:30:00Z"}


def test_extract_email_plain_and_slack_mailto():
    assert io.extract_email("its noah.walsey@assurant.com thx") == "noah.walsey@assurant.com"
    assert io.extract_email("<mailto:Noah@Assurant.com|Noah@Assurant.com>") == "noah@assurant.com"
    assert io.extract_email("no email here") == ""
    assert io.extract_email("me: zain@furtherai.com") == ""   # internal never counts


def test_needs_email_only_when_email_is_the_only_gap():
    assert io.needs_email(dict(_BASE), now=_NOW)
    assert not io.needs_email(dict(_BASE, prospect_email="n@assurant.com"), now=_NOW)
    assert not io.needs_email(dict(_BASE, organizer=""), now=_NOW)
    assert not io.needs_email(dict(_BASE, start_utc=""), now=_NOW)
    assert not io.needs_email(dict(_BASE, start_utc="2099-09-01T00:30:00Z"), now=_NOW)  # imminent
    assert not io.needs_email(dict(_BASE, start_utc="2099-08-01T00:00:00Z"), now=_NOW)  # past


def test_ask_text_names_the_prospect_and_says_how_to_reply():
    t = io.ask_email_text(dict(_BASE))
    assert io.EMAIL_ASK_MARK in t and "Noah Walsey" in t and "Assurant" in t
    assert "reply" in t.lower()
