"""VP+ ICP meeting escalation for meetingbuddy.

When a booked meeting has a VP+ ICP persona and neither Aman nor Zac is attached,
this module decides whether/how to bring an exec in. Reshaped per the 2026-09-16
council review: the default is a Slack PROPOSAL + read-only digest, NOT a silent
guest-add to the prospect's calendar invite (which emails the external prospect).

Modes (env VP_ESCALATION_MODE):
  digest  — only record the candidate to the exec feed. No thread post, no writes.
  propose — post a Slack proposal with [Add Zac]/[Add Aman]/[Skip] buttons. The
            calendar write happens ONLY on a human click. (default)
  auto    — silently add the freer exec. Stays OFF until explicitly enabled.

Safety flags:
  VP_ESCALATION_ENABLED (default "0") — master off switch.
  VP_ESCALATION_DRYRUN  (default "1") — log intended writes, perform none outward.

Google Calendar access is NEW and not wired here — `freebusy` / `add_guest` are
guarded stubs until OAuth scope is granted. Nothing in this module can email a
prospect while DRYRUN is on or ENABLED is off.

Safety-critical style: validated params, >=2 assertions/fn, bounded loops,
idempotent writes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

_CAL_API = "https://www.googleapis.com/calendar/v3"
_CAL_SCOPES = ("https://www.googleapis.com/auth/calendar.events",
               "https://www.googleapis.com/auth/calendar.freebusy")

# Canonical ICP loader. Prefer the vendored copy (icp_loader.py + icp_rules.yaml,
# synced from FAI/icp/) so this runs standalone in the Railway container; fall back
# to the monorepo path for local/dev use.
sys.path.insert(0, os.path.dirname(__file__))
try:
    import icp_loader as icp  # vendored (container)
except ImportError:  # pragma: no cover - dev fallback
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "icp"))
    import loader as icp

# The two execs, and the persisted round-robin marker property.
EXECS = ("aman", "zac")
_RR_STATE_FILE = os.path.join(os.path.dirname(__file__), ".vp_rr_state")
# HubSpot meeting property used as the idempotency marker (mirrors `booked_at`).
ESCALATION_MARKER = "vp_escalated_exec"


# ── Config helpers ───────────────────────────────────────────────────────────
def enabled() -> bool:
    return os.environ.get("VP_ESCALATION_ENABLED", "0") == "1"


def dry_run() -> bool:
    # Fail safe: anything other than an explicit "0" means dry-run.
    return os.environ.get("VP_ESCALATION_DRYRUN", "1") != "0"


def mode() -> str:
    # Default: nudge — @mention the booker to add Zac/Aman (no calendar access
    # needed). `auto`/`propose`/`digest` remain for the Google-Calendar path.
    m = os.environ.get("VP_ESCALATION_MODE", "nudge").strip().lower()
    assert m in ("nudge", "digest", "propose", "auto"), f"bad VP_ESCALATION_MODE: {m}"
    return m


# ── Candidate detection (pure, safe) ─────────────────────────────────────────
def is_escalation_candidate(meeting: dict, contact: dict) -> bool:
    """True iff this is a VP+ ICP meeting with no exec already attached.

    meeting: {"attendees": [str], "meeting_type": str, ...}
    contact: {"seniority": str, "segment": str, "employees": int|None,
              "function": str}
    """
    assert isinstance(meeting, dict), "meeting must be a dict"
    assert isinstance(contact, dict), "contact must be a dict"

    # Only demos by default (conference touches are 15-min, out of scope).
    if meeting.get("meeting_type", "demo") != "demo":
        return False

    # Already has an exec? Nothing to do (idempotent).
    if _exec_already_present(meeting):
        return False

    # Must clear the full canonical ICP: segment + size + seniority + function.
    if icp.segment_class(contact.get("segment", "")) != "icp":
        return False
    if not icp.role_allowed(contact.get("function", "")):
        return False
    if not icp.is_vp_plus(contact.get("seniority", "")):
        return False
    # VP+ at an ICP company is 'priority' at any size >= 50; sub-50 is floored out.
    return icp.tier("vp_plus", contact.get("employees")) == "priority"


def _exec_already_present(meeting: dict) -> bool:
    """True if Aman or Zac is already an attendee or already escalated."""
    if meeting.get(ESCALATION_MARKER):
        return True
    attendees = meeting.get("attendees", []) or []
    assert isinstance(attendees, list), "attendees must be a list"
    blob = " ".join(str(a).lower() for a in attendees[:50])  # bounded
    return any(name in blob for name in EXECS)


# ── Exec selection ───────────────────────────────────────────────────────────
def pick_exec(busy_minutes: dict) -> str:
    """Pick the freer exec; deterministic round-robin tiebreak (persisted).

    busy_minutes: {"aman": int, "zac": int} busy minutes in the meeting window /
    day. Lower = freer. On a tie, alternate via the persisted marker so it can't
    oscillate across Railway restarts.
    """
    assert set(busy_minutes) == set(EXECS), "busy_minutes must cover both execs"
    a, z = busy_minutes["aman"], busy_minutes["zac"]
    assert a >= 0 and z >= 0, "busy minutes must be non-negative"
    if a < z:
        return "aman"
    if z < a:
        return "zac"
    return _next_round_robin()


def _next_round_robin() -> str:
    """Return the next exec in rotation and advance the persisted pointer."""
    last = ""
    if os.path.exists(_RR_STATE_FILE):
        with open(_RR_STATE_FILE, "r", encoding="utf-8") as fh:
            last = fh.read().strip()
    nxt = "zac" if last == "aman" else "aman"
    if not dry_run():                      # only persist when acting for real
        with open(_RR_STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(nxt)
    return nxt


# ── Google Calendar side (NEW — guarded until OAuth granted) ──────────────────
def freebusy(exec_name: str, start_iso: str, end_iso: str) -> Optional[int]:
    """Busy minutes for an exec in [start,end]. None if GCal is not configured.

    Calls Calendar freeBusy and sums the busy periods clamped to the window.
    """
    assert exec_name in EXECS, f"unknown exec: {exec_name}"
    assert start_iso and end_iso, "window required"
    cal = _exec_calendar(exec_name)
    # Guard against a zero/negative window (callers often pass start==start): a
    # zero-width freeBusy query always returns no busy blocks, which would make
    # every exec look equally free and defeat the "pick the freer one" logic.
    win_lo = _parse_iso(start_iso)
    win_hi = _parse_iso(end_iso)
    if win_hi <= win_lo:
        win_hi = win_lo + timedelta(hours=1)   # assume a ~1h meeting window
    try:
        token = _gcal_token(subject=cal)  # impersonate the exec to read free/busy
        if not token:
            return None  # not configured -> caller falls back to round-robin
        import requests
        body = {"timeMin": win_lo.isoformat(), "timeMax": win_hi.isoformat(),
                "items": [{"id": cal}]}
        r = requests.post(f"{_CAL_API}/freeBusy", json=body,
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        cals = r.json().get("calendars", {})
        periods = next(iter(cals.values()), {}).get("busy", []) if cals else []
    except Exception:
        # DWD not authorized yet, network blip, scope issue, etc. — never crash the
        # bot over free/busy; fall back to round-robin selection.
        return None
    total = 0
    for p in periods[:200]:                 # bounded loop (Power-of-Ten rule 2)
        lo = max(win_lo, _parse_iso(p["start"]))
        hi = min(win_hi, _parse_iso(p["end"]))
        if hi > lo:
            total += int((hi - lo).total_seconds() // 60)
    return total


def add_guest(event_id: str, exec_name: str, calendar_id: Optional[str] = None) -> dict:
    """Add an exec as a guest on the real GCal event. Idempotent + guarded.

    Adds with `sendUpdates=none` so the external prospect is NOT emailed a "new
    attendee joined" notice — the exec still lands on the event (it shows on their
    calendar) and the Slack thread flag announces it. Refuses to act unless ENABLED
    and not DRYRUN, and no-ops if the exec is already on the event.

    calendar_id: the organizer/AE calendar the event lives on (an @furtherai.com
    address). Under domain-wide delegation the service account impersonates that
    organizer to patch their event. Falls back to VP_ORGANIZER_CALENDAR_ID.
    """
    assert event_id, "event_id required"
    assert exec_name in EXECS, f"unknown exec: {exec_name}"
    cal = calendar_id or os.environ.get("VP_ORGANIZER_CALENDAR_ID", "primary")
    intent = {"action": "add_guest", "event_id": event_id, "exec": exec_name,
              "calendar": cal, "send_updates": "none"}
    if not enabled() or dry_run():
        return {**intent, "performed": False, "reason": "disabled_or_dryrun"}
    no_dwd = os.environ.get("VP_CALENDAR_NO_DWD") == "1"
    subject = cal if "@" in cal else os.environ.get("VP_CALENDAR_SUBJECT", "")
    if not no_dwd and not subject:
        # DWD mode needs a user to impersonate; no-DWD mode acts as the SA on a
        # shared calendar, so no subject is required.
        return {**intent, "performed": False, "reason": "no_impersonation_subject"}
    token = _gcal_token(subject=subject or None)
    if not token:
        return {**intent, "performed": False, "reason": "gcal_not_configured"}

    import requests
    hdr = {"Authorization": f"Bearer {token}"}
    email = _exec_calendar(exec_name)
    ev = requests.get(f"{_CAL_API}/calendars/{cal}/events/{event_id}",
                      headers=hdr, timeout=15)
    ev.raise_for_status()
    attendees = ev.json().get("attendees", []) or []
    if any((a.get("email", "").lower() == email.lower()) for a in attendees[:100]):
        return {**intent, "performed": False, "reason": "already_present"}  # idempotent
    attendees.append({"email": email, "optional": True, "responseStatus": "accepted"})
    patch = requests.patch(
        f"{_CAL_API}/calendars/{cal}/events/{event_id}",
        params={"sendUpdates": "none"}, headers=hdr,
        json={"attendees": attendees}, timeout=15)
    patch.raise_for_status()
    return {**intent, "performed": True, "reason": "added"}


def _gcal_ready() -> bool:
    """True only when a real Google Calendar credential is present."""
    return bool(os.environ.get("GOOGLE_CALENDAR_TOKEN_JSON")
                or os.environ.get("GOOGLE_CALENDAR_TOKEN"))


def _exec_calendar(exec_name: str) -> str:
    """Calendar id (email) for an exec. Overridable via env; sensible defaults."""
    assert exec_name in EXECS, f"unknown exec: {exec_name}"
    env = f"VP_EXEC_CAL_{exec_name.upper()}"
    return os.environ.get(env, f"{exec_name}@furtherai.com")


def _gcal_token(subject: Optional[str] = None) -> Optional[str]:
    """Return a valid OAuth access token, refreshing if needed. None if unconfigured.

    Supports two credential shapes:
    - Service account (Option A, `"type": "service_account"`): uses domain-wide
      delegation to impersonate `subject` (an @furtherai.com user). subject required.
    - User OAuth token (Option B): sheet_sync.py's shape; `subject` is ignored.
    """
    raw = os.environ.get("GOOGLE_CALENDAR_TOKEN_JSON")
    path = os.environ.get("GOOGLE_CALENDAR_TOKEN")
    if not raw and path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    if not raw:
        return None
    info = json.loads(raw)
    from google.auth.transport.requests import Request
    if info.get("type") == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=list(_CAL_SCOPES))
        # With domain-wide delegation, impersonate the subject. Without admin/DWD
        # (VP_CALENDAR_NO_DWD=1), act as the service account itself and rely on
        # calendars being explicitly shared with the SA email.
        if subject and os.environ.get("VP_CALENDAR_NO_DWD") != "1":
            creds = creds.with_subject(subject)
    else:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=info.get("token"), refresh_token=info.get("refresh_token"),
            token_uri=info.get("token_uri"), client_id=info.get("client_id"),
            client_secret=info.get("client_secret"), scopes=info.get("scopes"))
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def _parse_iso(s: str) -> datetime:
    """Parse an RFC3339/ISO timestamp to an aware UTC datetime."""
    assert isinstance(s, str) and s, "timestamp required"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Note (HubSpot + event description) ───────────────────────────────────────
def build_note(exec_name: str, contact: dict, basis: str) -> str:
    """Human-readable note explaining who was added and why."""
    assert exec_name in EXECS, f"unknown exec: {exec_name}"
    assert isinstance(contact, dict), "contact must be a dict"
    who = exec_name.capitalize()
    seg = contact.get("segment", "?")
    fn = contact.get("function", "?")
    return (f"[VP+ escalation] {who} attached — VP+ ICP meeting "
            f"(segment={seg}, function={fn}, icp_version={icp.version()}). "
            f"Basis: {basis}.")


# ── Slack proposal blocks (posted by the bot; no side effects here) ──────────
def proposal_blocks(meeting: dict, contact: dict, suggested: str) -> list:
    """Build Slack Block Kit for the in-thread proposal. Human clicks decide."""
    assert suggested in EXECS, f"unknown suggested exec: {suggested}"
    company = meeting.get("company", "this account")
    title = contact.get("title", contact.get("function", "VP+"))
    text = (f":dart: *VP+ ICP meeting* — {title} at *{company}*. No exec attached.\n"
            f"Suggested: *{suggested.capitalize()}* (more free). Add one?")
    def _btn(label, value, style=None):
        b = {"type": "button", "text": {"type": "plain_text", "text": label},
             "value": value, "action_id": f"vp_escalate_{value}"}
        if style:
            b["style"] = style
        return b
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            _btn(f"Add {suggested.capitalize()}", suggested, "primary"),
            _btn(f"Add {_other(suggested).capitalize()}", _other(suggested)),
            _btn("Skip", "skip"),
        ]},
    ]


def _other(exec_name: str) -> str:
    assert exec_name in EXECS, f"unknown exec: {exec_name}"
    return "zac" if exec_name == "aman" else "aman"


# ── Parser → canonical ICP mapping ───────────────────────────────────────────
_SEGMENT_MAP = {"brokerage": "broker", "carrier": "p&c_carrier", "mga": "mga"}
_SENIORITY_KW = (                       # order matters: first hit wins (senior→junior)
    ("vp_plus", ("chief", "ceo", "cfo", "coo", "cto", "cio", "cro", "cxo",
                 "president", "founder", "owner", "partner", "svp", "evp",
                 "vice president", "vp")),
    ("director", ("head of", "avp", "assistant vice president", "senior director",
                  "sr director", "director", "deputy")),
    ("manager", ("manager", "lead", "supervisor")),
)
_FUNCTION_KW = (                         # deny functions first so they win
    ("corp_dev", ("corporate development", "corp dev", "m&a", "mergers")),
    ("cvc", ("ventures", "venture capital", "cvc", "corporate venture")),
    ("information_technology", ("information technology", "it", "software",
                                "engineer", "developer", "infrastructure")),
    ("leadership", ("ceo", "chief executive", "president", "founder", "owner",
                    "general manager", "managing director")),
    ("underwriting", ("underwriting", "underwriter")),
    ("claims", ("claims",)),
    ("distribution", ("distribution", "sales", "producer", "broker relations")),
    ("innovation", ("innovation", "digital", "transformation")),
    ("operations", ("operations", "operating", "ops", "coo")),
)


def _kw_hit(text: str, kws) -> bool:
    """True if any keyword appears with alphabetic word boundaries (so 'cto' does
    NOT match inside 'director'). Allows spaces/punctuation adjacency (&, -)."""
    for kw in kws:                      # bounded (Power-of-Ten rule 2)
        if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text):
            return True
    return False


def _norm_seniority(title: str) -> str:
    t = (title or "").lower()
    for key, kws in _SENIORITY_KW:      # bounded
        if _kw_hit(t, kws):
            return key
    return "ic"


def _norm_function(title: str) -> str:
    t = (title or "").lower()
    for key, kws in _FUNCTION_KW:       # bounded
        if _kw_hit(t, kws):
            return key
    return "leadership"                 # bias: C-level/unknown → leadership (allowed)


def _parse_employees(size) -> Optional[int]:
    """Parse company_size strings like '500', '10k', '~1,200' to an int."""
    if size is None:
        return None
    s = str(size).strip().lower().replace(",", "").replace("~", "").replace("+", "")
    if not s:
        return None
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        return int(float(s))
    except ValueError:
        return None


def contact_from_parsed(parsed: dict) -> dict:
    """Map meetingbuddy's parsed booking fields to the canonical contact shape."""
    assert isinstance(parsed, dict), "parsed must be a dict"
    seg = _SEGMENT_MAP.get((parsed.get("segment") or "").lower(), "")
    title = parsed.get("contact_title") or ""
    return {"seniority": _norm_seniority(title), "segment": seg,
            "function": _norm_function(title),
            "employees": _parse_employees(parsed.get("company_size")),
            "title": title}


def thread_flag(meeting: dict, contact: dict, added: str) -> str:
    """In-thread Slack message announcing the auto-add (posted by the bot)."""
    assert added in EXECS, f"unknown exec: {added}"
    company = meeting.get("company", "this account")
    title = contact.get("title", contact.get("function", "VP+"))
    return (f":dart: VP+ ICP meeting — {title} at *{company}*. "
            f"Auto-added *{added.capitalize()}* (more free) to the invite. "
            f"cc <@aman> <@zac> — swap if needed.")


def nudge_message(booker: Optional[str], meeting: dict, contact: dict) -> str:
    """@mention the booker asking them to add Zac or Aman to the invite. No
    calendar access needed — the booker does the add in their own calendar."""
    assert isinstance(contact, dict), "contact must be a dict"
    company = meeting.get("company") or "this account"
    title = contact.get("title") or contact.get("function") or "VP+"
    who = f"<@{booker}>" if booker else "team"
    return (f":dart: {who} — this looks like a *VP+ ICP* meeting "
            f"({title} at *{company}*). Please add *Zac* or *Aman* to the invite "
            f"so an exec can join. 🙏")


# ── Orchestrator: decide the action for one booked meeting ───────────────────
def handle_booked_meeting(meeting: dict, contact: dict, busy_minutes: Optional[dict] = None,
                          booker: Optional[str] = None) -> dict:
    """Return the escalation action for a booking. Performs NO Slack/GCal I/O.

    The caller (meeting_bot) executes the returned action. This keeps all decision
    logic pure and unit-testable, and guarantees no outward write happens here.
    Returns {"action": "none"|"nudge"|"digest"|"propose"|"auto_add", ...}.
    """
    if not enabled():
        return {"action": "none", "reason": "disabled"}
    if not is_escalation_candidate(meeting, contact):
        return {"action": "none", "reason": "not_candidate"}

    m = mode()
    if m == "nudge":                    # default — no calendar access needed
        return {"action": "nudge", "text": nudge_message(booker, meeting, contact)}

    # The remaining modes use the Google-Calendar path (need busy_minutes).
    suggested = pick_exec(busy_minutes or {"aman": 0, "zac": 0})
    if m == "digest":
        return {"action": "digest", "exec": suggested, "meeting": meeting.get("id")}
    if m == "propose":
        return {"action": "propose", "exec": suggested,
                "blocks": proposal_blocks(meeting, contact, suggested)}
    # auto: pure decision only. The caller resolves the real GCal event and adds
    # the exec, falling back to the nudge text if it can't. No I/O here.
    return {"action": "auto", "exec": suggested,
            "thread_flag": thread_flag(meeting, contact, suggested),
            "nudge_text": nudge_message(booker, meeting, contact),
            "note": build_note(suggested, contact, "auto mode, free/busy")}


# ── Calendar event resolution (needs domain-wide delegation) ─────────────────
def _event_matches(summary: str, attendee_emails, terms) -> bool:
    """True if any match term appears in the event summary or an attendee's email.
    Pure and unit-testable. Terms are matched case-insensitively as substrings."""
    hay = (summary or "").lower()
    emails = " ".join((e or "").lower() for e in (attendee_emails or [])[:50])
    for t in terms[:20]:                # bounded (Power-of-Ten rule 2)
        t = (t or "").strip().lower()
        if t and (t in hay or t in emails):
            return True
    return False


def find_calendar_event(search_as: str, start_iso: str, terms):
    """Locate the demo's GCal event by searching `search_as`'s calendar around the
    meeting time and matching on terms. Returns (event_id, organizer_email) or
    (None, None). Read-only; safe in dry-run. Requires DWD (impersonation)."""
    assert start_iso, "start_iso required"
    if not search_as or os.environ.get("VP_CALENDAR_NO_DWD") == "1":
        return (None, None)             # no-DWD mode can't impersonate to search
    try:
        token = _gcal_token(subject=search_as)
        if not token:
            return (None, None)
        import requests
        lo = _parse_iso(start_iso)
        # Search a window from 30 min before to 3h after the stated start. Use
        # timedelta (NOT .replace(hour=...), which collapses to zero width for
        # late-day times like 23:00 UTC and never rolls over the date).
        win_lo = (lo - timedelta(minutes=30)).isoformat()
        win_hi = (lo + timedelta(hours=3)).isoformat()
        r = requests.get(
            f"{_CAL_API}/calendars/{search_as}/events",
            params={"timeMin": win_lo, "timeMax": win_hi, "singleEvents": "true",
                    "orderBy": "startTime", "maxResults": 20},
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        for ev in r.json().get("items", [])[:20]:   # bounded
            emails = [a.get("email") for a in ev.get("attendees", []) or []]
            if _event_matches(ev.get("summary", ""), emails, terms):
                org = (ev.get("organizer") or {}).get("email") or search_as
                return (ev.get("id"), org)
    except Exception:
        return (None, None)             # never break the booking flow
    return (None, None)


def event_has_exec(organizer_email: str, event_id: str) -> bool:
    """True if Aman OR Zac is already a guest on the event (someone added one
    after the nudge). Lets the retry avoid a redundant second exec. Read-only."""
    if not (organizer_email and event_id):
        return False
    try:
        token = _gcal_token(subject=organizer_email)
        if not token:
            return False
        import requests
        r = requests.get(f"{_CAL_API}/calendars/{organizer_email}/events/{event_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        emails = " ".join((a.get("email", "") or "").lower()
                          for a in r.json().get("attendees", []) or [])
        return any(_exec_calendar(x).lower() in emails for x in EXECS)
    except Exception:
        return False


# ── Retry queue: keep trying to add the exec until the GCal invite syncs ──────
# The invite usually doesn't exist at Slack-post time, so a single attempt fails.
# We enqueue and retry for ESCALATION_TTL_SEC. In-memory (Railway disk is
# ephemeral); the immediate nudge is the floor if a restart drops the queue.
ESCALATION_TTL_SEC = 45 * 60
_PENDING = []
_PENDING_LOCK = threading.Lock()


def escalation_enqueue(ctx: dict) -> None:
    """Queue a pending auto-add. ctx needs meeting_id, exec, terms, start_iso,
    search_as, channel, thread_ts. Deduped by meeting_id."""
    assert ctx.get("meeting_id"), "meeting_id required"
    assert ctx.get("exec") in EXECS, "valid exec required"
    with _PENDING_LOCK:
        for e in _PENDING:                 # bounded by real booking volume
            if e["meeting_id"] == ctx["meeting_id"]:
                return                     # already queued
        ctx.setdefault("first_seen", time.time())
        _PENDING.append(ctx)


def escalation_pending() -> list:
    """Return non-expired pending items; prune expired ones in place."""
    now = time.time()
    with _PENDING_LOCK:
        _PENDING[:] = [e for e in _PENDING if now - e["first_seen"] < ESCALATION_TTL_SEC]
        return list(_PENDING)


def escalation_remove(meeting_id: str) -> None:
    with _PENDING_LOCK:
        _PENDING[:] = [e for e in _PENDING if e["meeting_id"] != meeting_id]
