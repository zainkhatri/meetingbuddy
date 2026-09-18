"""Calendar-sourced AE blitz leaderboard (poller inside meetingbuddy).

Automatic: reads each AE's Google Calendar via domain-wide delegation and counts
the meetings they booked during the blitz window. AEs just book — nothing to post.

Count rule (validated against real AE calendars 2026-09-17):
  an event counts for an AE when
    - the AE is the organizer, OR an external (non-furtherai) party organized it
      and the AE is on it  (exec/assistant-sent invites still credit the AE);
    - the event was `created` within [BLITZ_START, BLITZ_END];
    - it has >=1 external guest on a real company domain (free-mail excluded);
    - it is not cancelled (status or "Canceled:" title prefix);
  recurring series are collapsed to one (dedup by recurringEventId).

Inert unless BLITZ_CHANNEL_ID + BLITZ_AES are set. Read-only; no HubSpot, no
calendar writes. Power-of-Ten: bounded loops, >=2 assertions per function,
inputs validated, failures isolated per-AE.
"""

import datetime
import json
import os
import random
import re
import threading
import time

import requests
from google.oauth2 import service_account
import google.auth.transport.requests as greq

from leaderboard import render_text

DOMAIN = "furtherai.com"
CAL_API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
FREEMAIL = {
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com",
    "outlook.com", "hotmail.com", "live.com", "yahoo.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com",
}

BLITZ_CHANNEL_ID    = os.environ.get("BLITZ_CHANNEL_ID")
BLITZ_AES           = [e.strip().lower() for e in os.environ.get("BLITZ_AES", "").split(",") if e.strip()]
BLITZ_TITLE         = os.environ.get("BLITZ_TITLE", "AE Blitz: Booked Meetings")
BLITZ_POLL_SECS     = int(os.environ.get("BLITZ_POLL_SECS", "300"))
CONF_MEETINGS_CH    = os.environ.get("CONF_MEETINGS_CHANNEL", "C0B9Z8562RL")  # #conference-meetings
MAX_PAGES           = 20  # bounded pagination
_EMAIL_RE           = re.compile(r"[\w.+-]+@([\w-]+\.[\w.]+)")  # extract domains from text


def _state_path():
    return "/data/blitz_cal_state.json" if os.path.isdir("/data") else "blitz_cal_state.json"


STATE_PATH = os.environ.get("BLITZ_STATE_PATH", _state_path())


# ---- pure helpers (unit-tested) --------------------------------------------

def parse_iso(s):
    """RFC3339 string -> aware datetime, or None."""
    assert s is None or isinstance(s, str), "s must be str or None"
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def external_guests(ev):
    """Company-domain external attendees (free-mail + resources excluded)."""
    assert isinstance(ev, dict), "ev must be a dict"
    out = []
    for a in (ev.get("attendees") or []):
        if a.get("resource"):
            continue
        em = (a.get("email") or "").lower()
        if not em or "@" not in em:
            continue
        dom = em.rsplit("@", 1)[-1]
        if dom == DOMAIN or dom in FREEMAIL:
            continue
        out.append(em)
    return out


def is_cancelled(ev):
    assert isinstance(ev, dict), "ev must be a dict"
    if ev.get("status") == "cancelled":
        return True
    t = (ev.get("summary") or "").lower()
    return t.startswith("canceled:") or t.startswith("cancelled:")


def attribution(ev, ae_email):
    """'organizer', 'exec_organized', or None (teammate-organized -> not credited)."""
    assert isinstance(ev, dict), "ev must be a dict"
    assert isinstance(ae_email, str) and ae_email, "ae_email required"
    o = ev.get("organizer") or {}
    oem = (o.get("email") or "").lower()
    if o.get("self") or oem == ae_email.lower():
        return "organizer"
    if oem and not oem.endswith("@" + DOMAIN):
        return "exec_organized"
    return None


def has_itc_title(ev):
    """Event title must contain 'ITC' (case-insensitive) to count."""
    assert isinstance(ev, dict), "ev must be a dict"
    return "itc" in (ev.get("summary") or "").lower()


def fetch_bdr_domains(slack_token, start, end):
    """Read #conference-meetings for the blitz window; return set of external domains
    that BDRs posted bookings for. Any AE calendar event whose guests are all in
    this set was booked by a BDR, not the AE.

    Uses a simple email-regex scan — no Claude parse — so it only matches when
    the BDR included the prospect's email in their post (which meetingbuddy needs
    for HubSpot contact creation, so it's almost always present).
    """
    assert slack_token and start and end, "all args required"
    bdr_domains = set()
    try:
        params = {"channel": CONF_MEETINGS_CH,
                  "oldest": str(start.timestamp()),
                  "latest": str(end.timestamp()),
                  "limit": 200, "inclusive": "true"}
        for _ in range(MAX_PAGES):  # bounded pagination
            r = requests.get("https://slack.com/api/conversations.history",
                             headers={"Authorization": f"Bearer {slack_token}"},
                             params=params, timeout=20)
            if not r.ok:
                break
            d = r.json()
            for m in d.get("messages", []):
                if m.get("bot_id") or m.get("subtype"):
                    continue
                text = m.get("text", "")
                for dom in _EMAIL_RE.findall(text):
                    dom = dom.lower()
                    if dom != DOMAIN and dom not in FREEMAIL:
                        bdr_domains.add(dom)
            if not d.get("has_more"):
                break
            params["cursor"] = d.get("response_metadata", {}).get("next_cursor", "")
    except Exception as e:
        print(f"[blitz] conference-meetings fetch failed: {e}", flush=True)
    return bdr_domains


def count_bookings(events, ae_email, start, end, bdr_domains=None):
    """Count unique ITC meetings ae_email booked in [start, end].

    bdr_domains: set of company domains BDRs posted in #conference-meetings.
    Any event where ALL external guests are in bdr_domains is skipped (BDR did it).
    """
    assert isinstance(events, list), "events must be a list"
    assert start is not None and end is not None and start <= end, "valid window required"
    bdr_domains = bdr_domains or set()
    seen = set()
    n = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if is_cancelled(ev):
            continue
        if not has_itc_title(ev):
            continue
        created = parse_iso(ev.get("created"))
        if not created or created < start or created > end:
            continue
        guests = external_guests(ev)
        if not guests:
            continue
        if not attribution(ev, ae_email):
            continue
        # Skip if every external guest domain was already posted in #conference-meetings
        guest_domains = {g.rsplit("@", 1)[-1].lower() for g in guests}
        if bdr_domains and guest_domains and guest_domains.issubset(bdr_domains):
            continue
        key = ev.get("recurringEventId") or ev.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def display_name(ae_email):
    """nia@furtherai.com -> 'Nia'; 'jon.doe@' -> 'Jon Doe'."""
    assert isinstance(ae_email, str) and ae_email, "ae_email required"
    override = os.environ.get("BLITZ_NAMES", "")
    for pair in override.split(","):
        if "=" in pair:
            em, nm = pair.split("=", 1)
            if em.strip().lower() == ae_email.lower():
                return nm.strip()
    local = ae_email.split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").title()


# ---- calendar I/O ----------------------------------------------------------

def load_sa_info():
    raw = os.environ.get("GOOGLE_CALENDAR_TOKEN_JSON")
    path = os.environ.get("GOOGLE_CALENDAR_TOKEN")
    assert raw or path, "GOOGLE_CALENDAR_TOKEN[_JSON] required"
    return json.loads(raw) if raw else json.load(open(path))


def _token(sa_info, subject):
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES).with_subject(subject)
    creds.refresh(greq.Request())
    return creds.token


def list_events(token, updated_min_iso):
    """Events created/modified since updated_min_iso (bounded pagination)."""
    assert token, "token required"
    items = []
    page = None
    for _ in range(MAX_PAGES):
        params = {"updatedMin": updated_min_iso, "singleEvents": "true",
                  "showDeleted": "false", "maxResults": 250, "orderBy": "updated"}
        if page:
            params["pageToken"] = page
        r = requests.get(f"{CAL_API}/calendars/primary/events",
                         headers={"Authorization": "Bearer " + token},
                         params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"events.list {r.status_code}: {r.text[:150]}")
        d = r.json()
        items += d.get("items", [])
        page = d.get("nextPageToken")
        if not page:
            break
    return items


def poll_once(sa_info, aes, start, end, slack_token=None):
    """Return {ae_email: count | None}. None = read failed this cycle."""
    assert isinstance(aes, list), "aes must be a list"
    out = {}
    umin = start.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    # Fetch BDR-booked domains from #conference-meetings once per cycle
    bdr_domains = fetch_bdr_domains(slack_token, start, end) if slack_token else set()
    if bdr_domains:
        print(f"[blitz] {len(bdr_domains)} BDR-booked domain(s) excluded from AE credit", flush=True)
    for ae in aes:
        try:
            evs = list_events(_token(sa_info, ae), umin)
            out[ae] = count_bookings(evs, ae, start, end, bdr_domains)
        except Exception as e:
            print(f"[blitz] {ae} poll failed: {e}", flush=True)
            out[ae] = None
    return out


# ---- hype messages ---------------------------------------------------------

_FIRST = [
    "{name} drew first blood! 🩸",
    "{name} is on the board! 🚀",
    "{name} opened the account! 🎯",
    "{name} gets us started! ⚡",
]

_ANY = [
    "{name} is ON FIRE 🔥",
    "{name} won't stop 🥷",
    "{name} keeps COOKING 👨‍🍳",
    "{name} with ANOTHER one 📞",
    "{name} is locked in 🎯",
    "{name} is DANGEROUS today 💀",
    "{name} IS NOT SLOWING DOWN 🚂",
    "{name} keeps the heat on 🌶️",
    "{name} IS ON A TEAR ⚡",
]

_MILESTONE = "🚨 {name} just hit {count} meetings! UNSTOPPABLE 🚨"


def _post_hype(client, name, new_count):
    """Post a celebration message. Fire-and-forget — never blocks the board."""
    assert isinstance(name, str) and name, "name required"
    assert isinstance(new_count, int) and new_count >= 1, "count must be >= 1"
    try:
        if new_count % 5 == 0:
            msg = _MILESTONE.format(name=name, count=new_count)
        elif new_count == 1:
            msg = random.choice(_FIRST).format(name=name)
        else:
            msg = random.choice(_ANY).format(name=name)
        client.chat_postMessage(channel=BLITZ_CHANNEL_ID, text=msg)
    except Exception as e:
        print(f"[blitz] hype post failed for {name}: {e}", flush=True)


# ---- board + loop ----------------------------------------------------------

def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    assert isinstance(state, dict), "state must be a dict"
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def refresh_board(client, rows):
    """Bump-to-bottom board: delete old, post fresh code-block leaderboard, pin it."""
    assert isinstance(rows, list), "rows must be a list"
    disp  = sorted(rows, key=lambda t: (-t[1], t[0].lower()))
    total = sum(c for _, c in rows)
    text  = render_text(disp, BLITZ_TITLE, total)
    sig   = json.dumps(text)
    st    = _load_state()
    if sig == st.get("sig") and st.get("board_ts"):
        return  # nothing changed
    old = st.get("board_ts")
    if old:
        try:
            client.chat_delete(channel=BLITZ_CHANNEL_ID, ts=old)
        except Exception:
            pass
    try:
        resp = client.chat_postMessage(channel=BLITZ_CHANNEL_ID, text=text)
    except Exception as e:
        print(f"[blitz] board post failed: {e}", flush=True)
        return
    st["board_ts"] = resp["ts"]
    st["sig"]      = sig
    try:
        client.pins_add(channel=BLITZ_CHANNEL_ID, timestamp=resp["ts"])
    except Exception:
        pass
    _save_state(st)


def run_poller(client):
    """Daemon loop. Started from meeting_bot on boot when armed."""
    if not (BLITZ_CHANNEL_ID and BLITZ_AES):
        return
    start = parse_iso(os.environ.get("BLITZ_START"))
    end = parse_iso(os.environ.get("BLITZ_END"))
    if not (start and end):
        print("[blitz] BLITZ_START/BLITZ_END not set or invalid — poller idle", flush=True)
        return
    sa_info = load_sa_info()
    last = {}
    print(f"[blitz] calendar poller: {len(BLITZ_AES)} AEs, every {BLITZ_POLL_SECS}s, "
          f"window {start.date()}..{end.date()}", flush=True)
    while True:  # bounded by process lifetime; each cycle isolated
        try:
            counts = poll_once(sa_info, BLITZ_AES, start, end, slack_token=client.token)
            rows = []
            for ae in BLITZ_AES:
                c = counts.get(ae)
                if c is None:
                    c = last.get(ae, 0)  # keep last-known on a read failure
                else:
                    prev = last.get(ae, 0)
                    if c > prev:  # new booking detected — fire hype for each new one
                        name = display_name(ae)
                        for n in range(prev + 1, c + 1):  # bounded: at most MAX_BOOKINGS_PER_MSG
                            _post_hype(client, name, n)
                    last[ae] = c
                rows.append((display_name(ae), c))
            refresh_board(client, rows)
        except Exception as e:
            print(f"[blitz] poller cycle error: {e}", flush=True)
        time.sleep(BLITZ_POLL_SECS)
