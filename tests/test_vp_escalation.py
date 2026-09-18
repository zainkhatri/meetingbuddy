"""Tests for meetingbuddy VP+ escalation.

Focus on the safety-critical properties: candidate detection matches canonical
ICP, idempotency (never double-add), dry-run performs no outward write, and
round-robin is deterministic across simulated restarts.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vp_escalation as vp  # noqa: E402


def _vp_icp_contact(**over):
    c = {"seniority": "vp_plus", "segment": "broker", "employees": 800,
         "function": "operations", "title": "VP Operations"}
    c.update(over)
    return c


def _demo(**over):
    m = {"id": "m1", "meeting_type": "demo", "attendees": ["prospect@acme.com"],
         "company": "Acme Brokers", "event_id": "evt1"}
    m.update(over)
    return m


# ── Candidate detection ──────────────────────────────────────────────────────
def test_vp_icp_demo_is_candidate():
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact()) is True


def test_director_at_large_is_not_candidate():
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact(seniority="director")) is False


def test_sub_50_company_not_candidate():
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact(employees=20)) is False


def test_deny_segment_not_candidate():
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact(segment="life")) is False


def test_denied_function_not_candidate():
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact(function="corp_dev")) is False


def test_innovation_function_is_candidate():
    # Guards the exact bug from autoemail/NEXUS.
    assert vp.is_escalation_candidate(_demo(), _vp_icp_contact(function="innovation")) is True


def test_conference_meeting_skipped():
    assert vp.is_escalation_candidate(_demo(meeting_type="conference"), _vp_icp_contact()) is False


# ── Idempotency ──────────────────────────────────────────────────────────────
def test_exec_already_attendee_not_candidate():
    m = _demo(attendees=["prospect@acme.com", "aman@furtherai.com"])
    assert vp.is_escalation_candidate(m, _vp_icp_contact()) is False


def test_escalation_marker_blocks_reentry():
    m = _demo()
    m[vp.ESCALATION_MARKER] = "zac"
    assert vp.is_escalation_candidate(m, _vp_icp_contact()) is False


# ── Exec selection + round robin ─────────────────────────────────────────────
def test_pick_freer_exec():
    assert vp.pick_exec({"aman": 30, "zac": 120}) == "aman"
    assert vp.pick_exec({"aman": 200, "zac": 60}) == "zac"


def test_round_robin_alternates(tmp_path, monkeypatch):
    state = tmp_path / ".rr"
    monkeypatch.setattr(vp, "_RR_STATE_FILE", str(state))
    monkeypatch.setenv("VP_ESCALATION_DRYRUN", "0")  # allow persist
    first = vp.pick_exec({"aman": 50, "zac": 50})
    second = vp.pick_exec({"aman": 50, "zac": 50})
    assert first != second  # deterministic alternation


# ── Dry-run performs no outward write ────────────────────────────────────────
def test_add_guest_dryrun_does_nothing(monkeypatch):
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    monkeypatch.setenv("VP_ESCALATION_DRYRUN", "1")
    r = vp.add_guest("evt1", "zac")
    assert r["performed"] is False and r["reason"] == "disabled_or_dryrun"


def test_add_guest_disabled_does_nothing(monkeypatch):
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "0")
    monkeypatch.setenv("VP_ESCALATION_DRYRUN", "0")
    r = vp.add_guest("evt1", "zac")
    assert r["performed"] is False


# ── Orchestrator ─────────────────────────────────────────────────────────────
def test_handle_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "0")
    out = vp.handle_booked_meeting(_demo(), _vp_icp_contact(), {"aman": 10, "zac": 20})
    assert out["action"] == "none"


def test_handle_propose_builds_blocks(monkeypatch):
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    monkeypatch.setenv("VP_ESCALATION_MODE", "propose")
    out = vp.handle_booked_meeting(_demo(), _vp_icp_contact(), {"aman": 10, "zac": 20})
    assert out["action"] == "propose" and out["exec"] == "aman"
    assert any(b["type"] == "actions" for b in out["blocks"])


def test_handle_auto_is_pure_decision(monkeypatch):
    # auto mode returns a pure decision (no I/O); the caller resolves+adds the event.
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    monkeypatch.setenv("VP_ESCALATION_MODE", "auto")
    out = vp.handle_booked_meeting(_demo(), _vp_icp_contact(), {"aman": 10, "zac": 20},
                                   booker="U123")
    assert out["action"] == "auto"
    assert out["exec"] == "aman"                 # freer (10 < 20)
    assert "thread_flag" in out and "nudge_text" in out  # both, for add-or-fallback


def test_default_mode_is_nudge(monkeypatch):
    # Default (no MODE set): @mention the booker to add an exec. No calendar access.
    monkeypatch.delenv("VP_ESCALATION_MODE", raising=False)
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    out = vp.handle_booked_meeting(_demo(), _vp_icp_contact(), booker="U123")
    assert out["action"] == "nudge"
    assert "<@U123>" in out["text"]
    assert "Zac" in out["text"] and "Aman" in out["text"]


def test_nudge_falls_back_to_team_without_booker(monkeypatch):
    monkeypatch.delenv("VP_ESCALATION_MODE", raising=False)
    monkeypatch.setenv("VP_ESCALATION_ENABLED", "1")
    out = vp.handle_booked_meeting(_demo(), _vp_icp_contact(), booker=None)
    assert out["action"] == "nudge" and "team" in out["text"]


def test_event_matches_company_and_email():
    assert vp._event_matches("Demo: ePremium x FurtherAI", ["rob@epremium.com"], ["ePremium"]) is True
    assert vp._event_matches("Intro", ["s@bhguard.com"], ["bhguard.com"]) is True
    assert vp._event_matches("Team standup", ["a@furtherai.com"], ["ePremium"]) is False


def test_find_calendar_event_safe_without_dwd(monkeypatch):
    # no-DWD or missing subject -> (None, None), never raises.
    monkeypatch.setenv("VP_CALENDAR_NO_DWD", "1")
    assert vp.find_calendar_event("ae@furtherai.com", "2026-10-02T18:00:00Z", ["X"]) == (None, None)


# ── Retry queue (Bug 1 fix: keep trying until the invite syncs) ───────────────
def _ctx(mid="m1", who="zac"):
    return {"meeting_id": mid, "exec": who, "terms": ["Acme"],
            "start_iso": "2026-10-02T18:00:00Z", "search_as": "bdr@furtherai.com",
            "channel": "C1", "thread_ts": "123.45"}


def test_enqueue_and_pending():
    vp.escalation_remove("m1")
    vp.escalation_enqueue(_ctx())
    ids = [e["meeting_id"] for e in vp.escalation_pending()]
    assert "m1" in ids
    vp.escalation_remove("m1")
    assert "m1" not in [e["meeting_id"] for e in vp.escalation_pending()]


def test_enqueue_dedups_by_meeting():
    vp.escalation_remove("m2")
    vp.escalation_enqueue(_ctx("m2", "zac"))
    vp.escalation_enqueue(_ctx("m2", "aman"))  # same meeting -> ignored
    hits = [e for e in vp.escalation_pending() if e["meeting_id"] == "m2"]
    assert len(hits) == 1 and hits[0]["exec"] == "zac"
    vp.escalation_remove("m2")


def test_pending_expires(monkeypatch):
    vp.escalation_remove("m3")
    c = _ctx("m3")
    c["first_seen"] = time.time() - (vp.ESCALATION_TTL_SEC + 10)  # already expired
    vp.escalation_enqueue(c)
    assert "m3" not in [e["meeting_id"] for e in vp.escalation_pending()]


def test_add_guest_suppresses_prospect_email():
    # sendUpdates=none must always be the intent so the prospect isn't emailed.
    assert vp.add_guest("evt1", "zac")["send_updates"] == "none"


# ── Google Calendar wiring (safe without credentials) ────────────────────────
def test_freebusy_none_without_credentials(monkeypatch):
    # No cred -> None so pick_exec falls back to round-robin (never crashes bot).
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN", raising=False)
    assert vp.freebusy("aman", "2026-09-16T10:00:00Z", "2026-09-16T10:30:00Z") is None


def test_exec_calendar_default_and_override(monkeypatch):
    assert vp._exec_calendar("aman") == "aman@furtherai.com"
    monkeypatch.setenv("VP_EXEC_CAL_ZAC", "zac.k@furtherai.com")
    assert vp._exec_calendar("zac") == "zac.k@furtherai.com"


def test_parse_iso_handles_zulu():
    dt = vp._parse_iso("2026-09-16T10:00:00Z")
    assert dt.tzinfo is not None and dt.hour == 10


# ── Parser → ICP mapping (regression guards) ─────────────────────────────────
def test_director_not_misread_as_vp():
    # 'director' must NOT match 'cto' substring -> would falsely be vp_plus.
    assert vp._norm_seniority("Director, Claims") == "director"


def test_seniority_mapping():
    assert vp._norm_seniority("VP of Operations") == "vp_plus"
    assert vp._norm_seniority("Chief Underwriting Officer") == "vp_plus"
    assert vp._norm_seniority("Claims Manager") == "manager"
    assert vp._norm_seniority("Head of Distribution") == "director"
    assert vp._norm_seniority("Claims Adjuster") == "ic"


def test_function_mapping():
    assert vp._norm_function("VP Corporate Development") == "corp_dev"
    assert vp._norm_function("Director, Claims") == "claims"
    assert vp._norm_function("VP Underwriting") == "underwriting"


def test_parse_employees():
    assert vp._parse_employees("500") == 500
    assert vp._parse_employees("10k") == 10000
    assert vp._parse_employees("~1,200") == 1200
    assert vp._parse_employees(None) is None
    assert vp._parse_employees("unknown") is None


def test_contact_from_parsed_end_to_end():
    c = vp.contact_from_parsed({"contact_title": "VP of Operations",
                                "segment": "brokerage", "company_size": "500"})
    # This should be a real escalation candidate downstream.
    assert vp.is_escalation_candidate(_demo(), c) is True
    # A VP in Corp Dev should NOT be (denied function).
    cd = vp.contact_from_parsed({"contact_title": "VP Corporate Development",
                                 "segment": "carrier", "company_size": "3000"})
    assert vp.is_escalation_candidate(_demo(), cd) is False
