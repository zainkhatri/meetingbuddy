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
def test_existing_event_matched_by_attendee_email():
    evs = [{"summary": "Coffee", "attendees": [{"email": "TUnsworth@hanover.com"}]}]
    assert io.invite_exists_in(evs, "tunsworth@hanover.com", "The Hanover Insurance Group") is True


def test_existing_event_matched_by_company_in_title():
    evs = [{"summary": "FurtherAI + Hanover (ITC)", "attendees": []}]
    assert io.invite_exists_in(evs, "tunsworth@hanover.com", "The Hanover Insurance Group") is True


def test_unrelated_event_not_matched():
    evs = [{"summary": "Lunch", "attendees": [{"email": "a@b.com"}]},
           {"summary": "FurtherAI + Hanover", "status": "cancelled", "attendees": []}]
    assert io.invite_exists_in(evs, "tunsworth@hanover.com", "The Hanover Insurance Group") is False


# ── send: double click creates one event ─────────────────────────────────────
def test_send_twice_creates_one_event():
    created = []
    state = {"events": []}

    def fetch(_org, _lo, _hi):
        return list(state["events"])

    def insert(_org, body, _conf):
        created.append(body)
        state["events"].append({"summary": body["summary"], "attendees": body["attendees"]})
        return {"id": f"e{len(created)}", "htmlLink": "https://cal/e"}

    first = io.send_invite(_offer(), fetch_events=fetch, insert_event=insert)
    second = io.send_invite(_offer(), fetch_events=fetch, insert_event=insert)
    assert first["status"] == "sent"
    assert second["status"] == "already_exists"
    assert len(created) == 1


def test_send_reports_calendar_failure():
    def fetch(*_a):
        raise RuntimeError("boom")
    out = io.send_invite(_offer(), fetch_events=fetch, insert_event=lambda *a: None)
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
