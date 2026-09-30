"""Booking-post times: the model reports the time AS WRITTEN plus its zone, and
code converts with real tz rules. Asking the model for UTC broke twice at ITC:
USAA "Wednesday, September 30 @ 5 PM PST" was logged Tue 9/29 5pm (local date +
UTC time glued together), and "10AM PST" in September landed an hour late."""
import meeting_time as mt


def test_evening_pacific_meeting_keeps_its_day():
    # USAA: 5 PM Pacific on Wed 9/30 is 00:00 UTC on Thu 10/1.
    assert mt.start_utc_iso("2026-09-30", "17:00", "PST") == "2026-10-01T00:00:00Z"


def test_pst_in_september_means_pacific_daylight_time():
    # Apollo: "10AM PST" in late September = 10:00 PDT = 17:00 UTC (not 18:00).
    assert mt.start_utc_iso("2026-09-29", "10:00", "PST") == "2026-09-29T17:00:00Z"


def test_winter_pacific_and_other_zones():
    assert mt.start_utc_iso("2026-12-01", "10:00", "PT") == "2026-12-01T18:00:00Z"
    assert mt.start_utc_iso("2026-09-30", "10:00", "EST") == "2026-09-30T14:00:00Z"
    assert mt.start_utc_iso("2026-09-30", "10:00", "Central") == "2026-09-30T15:00:00Z"
    assert mt.start_utc_iso("2026-09-30", "10:00", "UTC") == "2026-09-30T10:00:00Z"


def test_unstated_zone_defaults_to_pacific():
    assert mt.start_utc_iso("2026-09-30", "17:00", None) == "2026-10-01T00:00:00Z"


def test_bad_input_returns_none():
    assert mt.start_utc_iso(None, "10:00", "PT") is None
    assert mt.start_utc_iso("2026-09-30", "25:99", "PT") is None
    assert mt.start_utc_iso("not a date", "10:00", "PT") is None


def test_normalize_fills_start_and_utc_time_from_local():
    b = {"meeting_date": "2026-09-30", "meeting_time_local": "17:00", "meeting_tz": "PST",
         "meeting_time_utc": "00:00"}                      # the model's (wrong-day) UTC is ignored
    mt.normalize_booking(b)
    assert b["meeting_start_utc"] == "2026-10-01T00:00:00Z"
    assert b["meeting_time_utc"] == "00:00" and b["meeting_date"] == "2026-09-30"


def test_normalize_legacy_utc_only_keeps_old_behaviour():
    b = {"meeting_date": "2026-09-30", "meeting_time_utc": "18:00"}
    mt.normalize_booking(b)
    assert b["meeting_start_utc"] == "2026-09-30T18:00:00Z"


def test_normalize_without_time_leaves_start_unset():
    b = {"meeting_date": "2026-09-30"}
    mt.normalize_booking(b)
    assert b.get("meeting_start_utc") is None


def test_invite_offer_uses_zone_correct_start():
    # The auto-invite glued local date + UTC time too — USAA's invite would have
    # been sent for Tue 9/29 5pm.
    import invite_offer
    b = mt.normalize_booking({"meeting_date": "2026-09-30", "meeting_time_local": "17:00", "meeting_tz": "PST",
                              "contact_first_name": "Mike", "contact_last_name": "Kyne", "company_name": "USAA"})
    offer = invite_offer.offer_from_booking(b, organizer="b@x.com", poster_slack="U1", ae_email="", ae_name="",
                                            is_conference=True, duration_min=30, conf_label="ITC")
    assert offer["start_utc"] == "2026-10-01T00:00:00Z"


def test_hs_create_meeting_prefers_start_iso(monkeypatch):
    import meeting_bot
    sent = {}
    class R:
        ok = True
        status_code = 201
        def json(self): return {"id": "m1"}
    def fake_post(url, **kw):
        sent.update(kw.get("json") or {}); return R()
    monkeypatch.setattr(meeting_bot.requests, "post", fake_post)
    meeting_bot.hs_create_meeting("t", "2026-09-30", "00:00", None, "o", "conference", "conference",
                                  "itc_2026", "", start_iso="2026-10-01T00:00:00Z")
    props = sent.get("properties") or {}
    assert props.get("hs_meeting_start_time") == str(1790812800000)


# --- audit 2026-09-29 ---
def test_unpadded_and_12h_times_are_kept():
    # "9:30" returned None → meeting created at the 14:00Z default (7am PT).
    assert mt.start_utc_iso("2026-09-30", "9:30", "PT") == "2026-09-30T16:30:00Z"
    assert mt.start_utc_iso("2026-09-30", "5:00 PM", "PT") == "2026-10-01T00:00:00Z"
    assert mt.start_utc_iso("2026-09-30", "12:15 am", "PT") == "2026-09-30T07:15:00Z"


def test_mst_is_fixed_utc_minus_7_like_vegas_now():
    # Reps in Vegas write "MST"; literally UTC-7 = the same wall clock as PDT in September.
    assert mt.start_utc_iso("2026-09-30", "16:30", "MST") == "2026-09-30T23:30:00Z"
    assert mt.start_utc_iso("2026-09-30", "16:30", "MDT") == "2026-09-30T22:30:00Z"


def test_spelled_out_and_iana_zones_are_recognised():
    for label in ("Eastern Time", "EDT (NYC)", "America/New_York", "eastern"):
        assert mt.start_utc_iso("2026-09-30", "10:00", label) == "2026-09-30T14:00:00Z", label
    assert mt.start_utc_iso("2026-09-30", "10:00", "Central Time") == "2026-09-30T15:00:00Z"
    assert mt.start_utc_iso("2026-09-30", "10:00", "10am ET / 7am PT") == "2026-09-30T14:00:00Z"


def test_reference_block_uses_post_date_in_pacific_and_lists_weekdays():
    # Post at Tue 9/29 6pm PT (= 01:00Z Wed). The old ref used the UTC date (Wed),
    # and the model resolved "Wednesday" to Thu 10/1.
    post_ts = 1790730000.0   # 2026-09-29 18:00 PT
    block = mt.reference_block(post_ts)
    assert "Post date: Tue 2026-09-29" in block
    assert "Wed 2026-09-30" in block and "Fri 2026-10-02" in block and "Tue 2026-10-06" in block


def test_pt_date_from_hubspot_iso_or_ms():
    assert mt.pt_date("2026-10-02T00:30:00Z") == "2026-10-01"
    assert mt.pt_date("1790812800000") == "2026-09-30"
    assert mt.pt_date("") is None
