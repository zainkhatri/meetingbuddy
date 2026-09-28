"""Calendar-invite offer for booking posts (#demos-booked, #conference-meetings).

After a booking is logged, if the meeting is not on the poster's or the AE's
calendar yet, the bot replies in-thread with a preview + Send/Skip buttons.
Send creates the event on the POSTER's calendar (poster = organizer) with only
the prospect and the owning AE as guests, and emails the invite.

Gated behind AUTO_INVITE (default off). Nothing reaches a prospect without a
human click. House style mirrors calendar_credit.py / vp_escalation.py:
validated params, >=2 assertions/fn, bounded loops, I/O injected for tests.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

import vp_escalation as vp

PT = ZoneInfo("America/Los_Angeles")
DOMAIN = "furtherai.com"
MAX_EVENTS = 250            # per calendar window fetch (bounded)
_PAYLOAD_KEYS = ("organizer", "poster_slack", "prospect_name", "prospect_email", "company",
                 "start_utc", "duration_min", "is_conference", "conf_short", "location",
                 "ae_email", "ae_name")
_GENERIC = {"the", "insurance", "group", "company", "companies", "co", "inc", "llc", "mutual",
            "holdings", "services", "of", "and", "agency", "associates", "partners"}

# Conference slug -> short tag for the invite title. Unknown slugs fall back to
# the HubSpot label the caller passes in.
CONF_SHORT = {"itc_2026": "ITC", "broker_tech_connect_2026": "BTC", "tmpaa": "TMPAA",
              "tmpcc": "TMPCC", "wsia_uw_summit": "WSIA", "wsia_dinner": "WSIA"}


def enabled() -> bool:
    return os.environ.get("AUTO_INVITE", "0").strip() == "1"


def admins() -> set:
    raw = os.environ.get("INVITE_ADMIN_SLACK_IDS", "U0AGP9NCBA5")  # Zain
    return {s.strip() for s in raw.split(",") if s.strip()}


# ── pure helpers ─────────────────────────────────────────────────────────────
def _parse_utc(s: str) -> datetime:
    assert isinstance(s, str) and s, "timestamp required"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, "timestamp must be tz-aware"
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    assert dt.tzinfo is not None, "dt must be tz-aware"
    out = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out.endswith("Z"), "must be UTC"
    return out


def fmt_pt(start_utc: str) -> str:
    """'2026-09-30T19:00:00Z' -> 'Wed Sep 30, 12:00 PM PT'."""
    assert isinstance(start_utc, str) and start_utc, "start_utc required"
    t = _parse_utc(start_utc).astimezone(PT)
    hour = t.hour % 12 or 12
    out = f"{t:%a} {t:%b} {t.day}, {hour}:{t:%M} {'AM' if t.hour < 12 else 'PM'} PT"
    assert out.endswith("PT"), "format"
    return out


def offer_from_booking(parsed: dict, *, organizer: str, poster_slack: str, ae_email: str,
                       ae_name: str, is_conference: bool, duration_min: int, conf_label: str) -> dict:
    """Map a parsed booking post to the offer dict carried in the button payload."""
    assert isinstance(parsed, dict), "parsed must be a dict"
    assert isinstance(duration_min, int) and duration_min > 0, "duration_min must be > 0"
    date, hhmm = parsed.get("meeting_date") or "", parsed.get("meeting_time_utc") or ""
    start = f"{date}T{hhmm}:00Z" if date and re.fullmatch(r"\d{1,2}:\d{2}", hhmm) else ""
    if start and len(hhmm) == 4:
        start = f"{date}T0{hhmm}:00Z"
    name = " ".join(x for x in (parsed.get("contact_first_name"), parsed.get("contact_last_name")) if x)
    conf = parsed.get("conference_source") or ""
    return {"organizer": (organizer or "").strip().lower(), "poster_slack": poster_slack or "",
            "prospect_name": name, "prospect_email": (parsed.get("contact_email") or "").strip().lower(),
            "company": parsed.get("company_name") or "", "start_utc": start,
            "duration_min": duration_min, "is_conference": bool(is_conference),
            "conf_short": (CONF_SHORT.get(conf) or conf_label or "") if is_conference else "",
            "location": (parsed.get("location") or "").strip() if is_conference else "",
            "ae_email": (ae_email or "").strip().lower(), "ae_name": ae_name or ""}


def decide_offer(offer: dict, *, invite_exists: Optional[bool], now: datetime):
    """(should_offer, reason). invite_exists None = calendar check failed."""
    assert isinstance(offer, dict), "offer must be a dict"
    assert now.tzinfo is not None, "now must be tz-aware"
    if not offer.get("organizer"):
        return (False, "no_organizer")
    email = (offer.get("prospect_email") or "").strip().lower()
    if not email or "@" not in email:
        return (False, "no_email")
    if email.endswith("@" + DOMAIN):
        return (False, "internal_email")
    if not offer.get("start_utc"):
        return (False, "no_time")
    if _parse_utc(offer["start_utc"]) <= now:
        return (False, "past")
    if invite_exists is None:
        return (False, "calendar_unknown")
    if invite_exists:
        return (False, "invite_exists")
    return (True, "ok")


def event_title(offer: dict) -> str:
    assert isinstance(offer, dict), "offer must be a dict"
    company = (offer.get("company") or offer.get("prospect_name") or "Meeting").strip()
    title = f"FurtherAI + {company}"
    if offer.get("is_conference") and offer.get("conf_short"):
        title += f" ({offer['conf_short']})"
    assert title.startswith("FurtherAI + "), "title prefix"
    return title


def build_event(offer: dict) -> dict:
    """Google Calendar event body. Organizer is implicit (the impersonated poster);
    guests are ONLY the prospect and the AE (never the organizer)."""
    assert isinstance(offer, dict), "offer must be a dict"
    assert offer.get("start_utc") and offer.get("prospect_email"), "start + email required"
    start = _parse_utc(offer["start_utc"])
    end = start + timedelta(minutes=int(offer.get("duration_min") or 30))
    org = (offer.get("organizer") or "").lower()
    guests = [{"email": offer["prospect_email"], "displayName": offer.get("prospect_name") or ""}]
    ae = (offer.get("ae_email") or "").strip().lower()
    if ae and ae != org and ae != offer["prospect_email"].lower():
        guests.append({"email": ae})
    body = {"summary": event_title(offer),
            "description": f"Meeting with {offer.get('prospect_name') or 'prospect'}"
                           f" ({offer.get('company') or ''}). Booked via meetingbuddy.",
            "start": {"dateTime": _iso_z(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso_z(end), "timeZone": "UTC"},
            "attendees": guests}
    if offer.get("is_conference"):
        body["location"] = (offer.get("location") or "").strip() or "FurtherAI Booth"
    else:
        rid = re.sub(r"[^a-zA-Z0-9]", "", f"mb{offer['start_utc']}{offer['prospect_email']}")[:60]
        body["conferenceData"] = {"createRequest": {
            "requestId": rid, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


def encode_payload(offer: dict) -> str:
    assert isinstance(offer, dict), "offer must be a dict"
    raw = json.dumps({k: offer.get(k) for k in _PAYLOAD_KEYS}, separators=(",", ":"))
    assert len(raw) <= 2000, "Slack button value limit"
    return raw


def decode_payload(raw: str) -> Optional[dict]:
    assert raw is None or isinstance(raw, str), "raw must be str"
    try:
        d = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not all(k in d for k in _PAYLOAD_KEYS):
        return None
    return d


def may_click(user_id: str, offer: dict, *, admins: set) -> bool:
    assert isinstance(offer, dict), "offer must be a dict"
    assert isinstance(admins, (set, frozenset)), "admins must be a set"
    if not user_id:
        return False
    return user_id == offer.get("poster_slack") or user_id in admins


def _company_core(company: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9&]+", (company or "").lower()) if w not in _GENERIC]
    return words[0] if words and len(words[0]) >= 3 else ""


def invite_exists_in(events, prospect_email: str, company: str) -> bool:
    """True if any non-cancelled event has the prospect as a guest or names the
    company in its title."""
    assert isinstance(events, list), "events must be a list"
    assert isinstance(prospect_email, str), "prospect_email must be str"
    email = prospect_email.strip().lower()
    core = _company_core(company)
    for ev in events[:MAX_EVENTS]:   # bounded
        if ev.get("status") == "cancelled":
            continue
        guests = {(a.get("email") or "").lower() for a in (ev.get("attendees") or [])}
        if email and email in guests:
            return True
        if core and re.search(rf"\b{re.escape(core)}\b", (ev.get("summary") or "").lower()):
            return True
    return False


def preview_blocks(offer: dict) -> list:
    assert isinstance(offer, dict), "offer must be a dict"
    assert offer.get("start_utc"), "start_utc required"
    ae = offer.get("ae_name") or offer.get("ae_email")
    ae_line = f"*AE:* {ae}" if ae else "*AE:* _No AE owner in HubSpot, so only the prospect is invited_"
    where = (offer.get("location") or "FurtherAI Booth") if offer.get("is_conference") else "Google Meet"
    text = (f":calendar: *No calendar invite found for this one.* Want me to send it?\n"
            f"*{event_title(offer)}*\n"
            f"*When:* {fmt_pt(offer['start_utc'])} ({offer.get('duration_min') or 30} min)\n"
            f"*Where:* {where}\n"
            f"*Prospect:* {offer.get('prospect_name') or '—'} ({offer['prospect_email']})\n"
            f"{ae_line}\n"
            f"_Sent from your calendar. Only the prospect + AE are guests._")
    raw = encode_payload(offer)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "style": "primary", "action_id": "invite_send",
                 "text": {"type": "plain_text", "text": "Send invite"}, "value": raw},
                {"type": "button", "action_id": "invite_skip",
                 "text": {"type": "plain_text", "text": "Skip"}, "value": raw}]}]


def send_invite(offer: dict, *, fetch_events: Callable, insert_event: Callable) -> dict:
    """Re-check the organizer's calendar, then create. Idempotent: a second call
    (double click, retry) finds the first event and does nothing."""
    assert isinstance(offer, dict), "offer must be a dict"
    assert callable(fetch_events) and callable(insert_event), "I/O fns required"
    start = _parse_utc(offer["start_utc"])
    lo, hi = _iso_z(start - timedelta(days=1)), _iso_z(start + timedelta(days=1))
    try:
        evs = fetch_events(offer["organizer"], lo, hi)
        if evs is None:
            return {"status": "error", "detail": "calendar read failed"}
        if invite_exists_in(evs, offer["prospect_email"], offer.get("company") or ""):
            return {"status": "already_exists"}
        body = build_event(offer)
        created = insert_event(offer["organizer"], body, "conferenceData" in body)
    except Exception as e:  # never raise into the Slack handler
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"[:200]}
    if not created or not created.get("id"):
        return {"status": "error", "detail": "calendar insert failed"}
    return {"status": "sent", "event_id": created["id"], "link": created.get("htmlLink", "")}


# ── I/O (Google Calendar via vp_escalation's DWD token; Apollo) ──────────────
def gcal_fetch_events(calendar_email: str, time_min: str, time_max: str) -> Optional[list]:
    """Events on `calendar_email` in [time_min, time_max]. None on failure."""
    assert calendar_email and time_min and time_max, "args required"
    assert calendar_email.endswith("@" + DOMAIN), "only internal calendars"
    token = vp._gcal_token(subject=calendar_email)
    if not token:
        return None
    r = requests.get(f"{vp._CAL_API}/calendars/primary/events", timeout=15,
                     headers={"Authorization": f"Bearer {token}"},
                     params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                             "maxResults": MAX_EVENTS, "showDeleted": "false"})
    if r.status_code != 200:
        print(f"[invite] events.list {calendar_email} -> {r.status_code}", flush=True)
        return None
    return r.json().get("items", [])


def gcal_insert_event(organizer: str, body: dict, needs_conference: bool) -> Optional[dict]:
    assert organizer.endswith("@" + DOMAIN), "organizer must be internal"
    assert isinstance(body, dict) and body.get("attendees"), "body with guests required"
    token = vp._gcal_token(subject=organizer)
    if not token:
        return None
    params = {"sendUpdates": "all"}
    if needs_conference:
        params["conferenceDataVersion"] = "1"
    r = requests.post(f"{vp._CAL_API}/calendars/primary/events", timeout=20,
                      headers={"Authorization": f"Bearer {token}"}, params=params, json=body)
    if r.status_code not in (200, 201):
        print(f"[invite] events.insert {organizer} -> {r.status_code} {r.text[:200]}", flush=True)
        return None
    return r.json()


def invite_exists(calendars, prospect_email: str, company: str, start_utc: str) -> Optional[bool]:
    """Check each internal calendar (organizer, AE) within +/-1 day. None if any
    read failed (caller treats unknown as 'don't offer')."""
    assert start_utc, "start_utc required"
    assert isinstance(prospect_email, str), "prospect_email must be str"
    start = _parse_utc(start_utc)
    lo, hi = _iso_z(start - timedelta(days=1)), _iso_z(start + timedelta(days=1))
    for cal in [c for c in dict.fromkeys(calendars) if c and c.endswith("@" + DOMAIN)][:4]:
        try:
            evs = gcal_fetch_events(cal, lo, hi)
        except Exception:
            evs = None
        if evs is None:
            return None
        if invite_exists_in(evs, prospect_email, company):
            return True
    return False


def apollo_email(first: str, last: str, company: str) -> str:
    """Verified work email from Apollo people/match, or ''."""
    assert isinstance(company, str), "company must be str"
    key = os.environ.get("APOLLO_API_KEY", "")
    if not key or not (first or last) or not company:
        return ""
    try:
        r = requests.post("https://api.apollo.io/api/v1/people/match", timeout=20,
                          headers={"X-Api-Key": key, "Content-Type": "application/json"},
                          json={"first_name": first or "", "last_name": last or "",
                                "organization_name": company})
        if r.status_code != 200:
            print(f"[invite] apollo people/match -> {r.status_code}", flush=True)
            return ""
        p = (r.json() or {}).get("person") or {}
    except Exception:
        return ""
    email = (p.get("email") or "").strip().lower()
    ok = p.get("email_status") in ("verified", "likely_to_engage") and "@" in email
    return email if ok and "email_not_unlocked" not in email else ""
