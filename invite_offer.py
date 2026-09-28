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
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

import vp_escalation as vp

PT = ZoneInfo("America/Los_Angeles")
DOMAIN = "furtherai.com"
MAX_EVENTS = 250            # per calendar window fetch (bounded)
WINDOW_DAYS = 3             # invites drift up to 2 days from the Slack post
IMMINENT_MIN = 60           # never offer for a meeting starting within the hour
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
    start = _parse_utc(offer["start_utc"])
    if start <= now:
        return (False, "past")
    if start - now < timedelta(minutes=IMMINENT_MIN):
        return (False, "imminent")   # booked on the spot: they're handling it live
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
    words = [w for w in re.findall(r"[a-z0-9&]+", (company or "").lower())
             if w not in _GENERIC and len(w) >= 3]
    return words[0] if words else ""


def _last_name(name: str) -> str:
    parts = re.findall(r"[a-z'-]+", (name or "").lower())
    return parts[-1] if len(parts) >= 2 and len(parts[-1]) >= 3 else ""


def invite_exists_in(events, prospect_email: str, company: str, prospect_name: str = "") -> bool:
    """True if any non-cancelled event already covers this prospect:
    (1) prospect email is a guest; (2) prospect last name in the title or a guest's
    display name (ITC-app/grip invites carry no prospect email); (3) company core
    word in the title, UNLESS that event's guests include a different person at the
    prospect's domain (a second contact at the same company is a separate meeting)."""
    assert isinstance(events, list), "events must be a list"
    assert isinstance(prospect_email, str), "prospect_email must be str"
    email = prospect_email.strip().lower()
    dom = email.split("@", 1)[1] if "@" in email else ""
    core, last = _company_core(company), _last_name(prospect_name)
    for ev in events[:MAX_EVENTS]:   # bounded
        if ev.get("status") == "cancelled":
            continue
        att = ev.get("attendees") or []
        guests = {(a.get("email") or "").lower() for a in att}
        if email and email in guests:
            return True
        title = (ev.get("summary") or "").lower()
        names = " ".join((a.get("displayName") or "").lower() for a in att)
        if last and re.search(rf"\b{re.escape(last)}\b", f"{title} {names}"):
            return True
        # name only in the guest's address: bseiter@, jen.jennings@, keishasmith@
        if last and any(last in g.split("@", 1)[0] for g in guests
                        if g and not g.endswith("@" + DOMAIN) and "gripcontact" not in g):
            return True
        other_contact = dom and any(g.endswith("@" + dom) and g != email for g in guests)
        if core and not other_contact and re.search(rf"\b{re.escape(core)}\b", title):
            return True
    return False


def team_calendars(organizer: str, ae_email: str, roster) -> list:
    """Internal calendars to scan: organizer and AE first, then the whole AE/BDR
    roster (invites are often sent by a teammate, not the poster)."""
    assert isinstance(roster, (list, tuple, set)), "roster must be a collection"
    out = []
    for c in [organizer, ae_email, *list(roster)[:60]]:   # bounded
        c = (c or "").strip().lower()
        if c.endswith("@" + DOMAIN) and c not in out:
            out.append(c)
    assert len(out) == len(set(out)), "deduped"
    return out


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


def send_invite(offer: dict, *, check_exists: Callable, insert_event: Callable) -> dict:
    """Re-run the team-wide check, then create. Idempotent: a second call (double
    click, retry, teammate sent it meanwhile) finds the event and does nothing.
    An unknown check result never sends."""
    assert isinstance(offer, dict), "offer must be a dict"
    assert callable(check_exists) and callable(insert_event), "I/O fns required"
    try:
        exists = check_exists(offer)
        if exists is None:
            return {"status": "error", "detail": "couldn't read the team calendars"}
        if exists:
            return {"status": "already_exists"}
        body = build_event(offer)
        created = insert_event(offer["organizer"], body, "conferenceData" in body)
    except Exception as e:  # never raise into the Slack handler
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"[:200]}
    if not created or not created.get("id"):
        return {"status": "error", "detail": "calendar insert failed"}
    return {"status": "sent", "event_id": created["id"], "link": created.get("htmlLink", "")}


# ── I/O (Google Calendar via vp_escalation's DWD token; Apollo) ──────────────
_TOKENS = {}          # subject -> (token, minted_at); DWD tokens live 60 min
_TOKEN_TTL_S = 45 * 60


def _token(subject: str) -> Optional[str]:
    """Cached delegated token: a team-wide check reads ~30 calendars."""
    assert subject and "@" in subject, "subject required"
    hit = _TOKENS.get(subject)
    if hit and time.time() - hit[1] < _TOKEN_TTL_S:
        return hit[0]
    tok = vp._gcal_token(subject=subject)
    if tok:
        _TOKENS[subject] = (tok, time.time())
    assert tok is None or isinstance(tok, str), "token must be str"
    return tok


def gcal_fetch_events(calendar_email: str, time_min: str, time_max: str) -> Optional[list]:
    """Events on `calendar_email` in [time_min, time_max]. None = permanent miss
    (not a Workspace user / 4xx). Raises on transient failure (network, 5xx)."""
    assert calendar_email and time_min and time_max, "args required"
    assert calendar_email.endswith("@" + DOMAIN), "only internal calendars"
    try:
        token = _token(calendar_email)
    except Exception as e:
        if "invalid_grant" in str(e):
            return None   # permanent: not a real Workspace user (alias, departed)
        raise             # transient: caller treats as unknown
    if not token:
        return None
    r = None
    for attempt in range(2):   # one retry: googleapis reads occasionally time out
        try:
            r = requests.get(f"{vp._CAL_API}/calendars/primary/events", timeout=15,
                             headers={"Authorization": f"Bearer {token}"},
                             params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                                     "maxResults": MAX_EVENTS, "showDeleted": "false"})
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 1:
                raise
            continue
        if r.status_code < 500:
            break
    if r.status_code >= 500:
        raise RuntimeError(f"events.list {calendar_email} -> {r.status_code}")
    if r.status_code != 200:
        print(f"[invite] events.list {calendar_email} -> {r.status_code}", flush=True)
        return None   # 4xx: permanent (no calendar / no access)
    return r.json().get("items", [])


def gcal_insert_event(organizer: str, body: dict, needs_conference: bool) -> Optional[dict]:
    assert organizer.endswith("@" + DOMAIN), "organizer must be internal"
    assert isinstance(body, dict) and body.get("attendees"), "body with guests required"
    token = _token(organizer)
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


def team_roster_from_hubspot(*, api_key: str, http_get: Callable = None) -> list:
    """Every active @furtherai.com HubSpot owner (AEs, BDRs, execs like Zac/Aman,
    EU reps): any of them may have sent the invite. Placeholders excluded."""
    assert api_key, "api_key required"
    http_get = http_get or requests.get
    assert callable(http_get), "http_get must be callable"
    out, after = [], None
    for _ in range(20):   # <= 2000 owners, hard cap
        params = {"limit": 100}
        if after:
            params["after"] = after
        r = http_get("https://api.hubapi.com/crm/v3/owners", headers={"Authorization": f"Bearer {api_key}"},
                     params=params, timeout=30)
        if not (r is not None and getattr(r, "ok", False)):
            break
        j = r.json()
        for o in j.get("results", []):
            em = (o.get("email") or "").strip().lower()
            if (em.endswith("@" + DOMAIN) and not o.get("archived") and em not in out
                    and not any(w in em for w in ("unassigned", "disqualif", "queue"))):
                out.append(em)
        after = j.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out[:60]


def invite_exists(calendars, prospect_email: str, company: str, start_utc: str,
                  prospect_name: str = "", required: int = 2) -> Optional[bool]:
    """Scan each internal calendar within +/-WINDOW_DAYS. The first `required`
    calendars (organizer, AE) must be readable or the answer is None (callers treat
    unknown as 'don't offer' / 'don't send'); the rest of the roster is best-effort."""
    assert start_utc, "start_utc required"
    assert isinstance(prospect_email, str), "prospect_email must be str"
    start = _parse_utc(start_utc)
    lo = _iso_z(start - timedelta(days=WINDOW_DAYS))
    hi = _iso_z(start + timedelta(days=WINDOW_DAYS))
    cals = [c for c in dict.fromkeys(calendars) if c and c.endswith("@" + DOMAIN)][:60]

    def read(cal):
        err = None
        for _ in range(2):   # one retry per calendar for transient blips
            try:
                return gcal_fetch_events(cal, lo, hi), None
            except Exception as e:
                err = e
        return None, err

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:   # bounded parallel reads
        results = list(pool.map(read, cals))
    for i, (cal, (evs, err)) in enumerate(zip(cals, results)):
        if err is not None:
            print(f"[invite] calendar read failed {cal}: {type(err).__name__}", flush=True)
            return None   # transient: can't rule out a teammate's invite
        if evs is None:
            if i < required:
                return None
            continue      # permanent: this user has no calendar to check
        if invite_exists_in(evs, prospect_email, company, prospect_name):
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
