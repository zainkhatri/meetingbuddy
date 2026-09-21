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
_EMAIL_RE           = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")  # extract full emails from text

# Known BDR/SDR emails — any calendar event with one of these as an attendee
# (when the AE is NOT the organizer) is a BDR-sourced meeting and is excluded.
# Configurable via BLITZ_BDRS env var (comma-separated) to avoid hardcoding.
_DEFAULT_BDRS = "zain@furtherai.com,jacob@furtherai.com,daniella@furtherai.com,benjamin.t@furtherai.com,matthew@furtherai.com"
BDR_EMAILS = {e.strip().lower() for e in os.environ.get("BLITZ_BDRS", _DEFAULT_BDRS).split(",") if e.strip()}


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


def has_bdr_attendee(ev, ae_email):
    """Return True if a known BDR is on the invite AND the AE is not the organizer.

    If the AE organized the meeting and invited a BDR for support, that's the AE's
    work — don't exclude it. But if the AE is just an attendee and a BDR is also
    there, the BDR booked it.

    This is the primary BDR exclusion signal — it's instantaneous (no timing race).
    """
    assert isinstance(ev, dict), "ev must be a dict"
    assert isinstance(ae_email, str) and ae_email, "ae_email required"
    o = ev.get("organizer") or {}
    ae_is_organizer = o.get("self") or (o.get("email") or "").lower() == ae_email.lower()
    if ae_is_organizer:
        return False  # AE organized it — BDR is just support
    for a in (ev.get("attendees") or []):
        if (a.get("email") or "").lower() in BDR_EMAILS:
            return True
    return False


def fetch_bdr_contacts(slack_token, start, end):
    """Read #conference-meetings for the blitz window; return set of specific contact
    emails BDRs posted bookings for.

    Email-level (not domain-level): a BDR booking jane@acme.com does NOT block an
    AE from getting credit for booking john@acme.com — a different person at the
    same company. Only the exact person booked by a BDR is excluded.

    Extends window to now so late Slack posts (BDRs post after creating the calendar
    event) are always caught regardless of when the poller runs.
    """
    assert slack_token and start and end, "all args required"
    bdr_contacts = set()
    now = datetime.datetime.now(datetime.timezone.utc)
    read_until = max(end, now)
    try:
        params = {"channel": CONF_MEETINGS_CH,
                  "oldest": str(start.timestamp()),
                  "latest": str(read_until.timestamp()),
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
                for email in _EMAIL_RE.findall(text):
                    email = email.lower()
                    dom = email.rsplit("@", 1)[-1]
                    if dom != DOMAIN and dom not in FREEMAIL:
                        bdr_contacts.add(email)
            if not d.get("has_more"):
                break
            params["cursor"] = d.get("response_metadata", {}).get("next_cursor", "")
    except Exception as e:
        print(f"[blitz] conference-meetings fetch failed: {e}", flush=True)
    return bdr_contacts


def count_bookings(events, ae_email, start, end, bdr_contacts=None):
    """Count unique ITC meetings ae_email booked in [start, end].

    bdr_contacts: set of specific contact emails BDRs posted in #conference-meetings.
    An event is skipped only if ALL of its external guests were individually booked
    by a BDR — a different person at the same company is fine.
    """
    assert isinstance(events, list), "events must be a list"
    assert start is not None and end is not None and start <= end, "valid window required"
    bdr_contacts = bdr_contacts or set()
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
        # Primary BDR check (no timing race): BDR on invite + AE not the organizer
        if has_bdr_attendee(ev, ae_email):
            continue
        # Secondary BDR check (email-level, not domain): all specific guest emails
        # were individually posted in #conference-meetings by a BDR.
        # A different person at the same company is fine — must be exact email match.
        guest_emails = {g.lower() for g in guests}
        if bdr_contacts and guest_emails and guest_emails.issubset(bdr_contacts):
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


_RETRYABLE = (429, 500, 502, 503, 504)  # transient — worth another attempt


def _get_events_page(token, params, attempts=3):
    """Fetch one events page with bounded retries on timeout / transient 5xx.

    The Railway container occasionally hits slow googleapis reads; a single
    30s hang used to stall the whole poll cycle. Short timeout + a couple of
    retries keeps a flaky calendar from blocking the board.
    """
    assert token, "token required"
    assert attempts >= 1, "attempts must be >= 1"
    last_err = None
    for i in range(attempts):  # bounded
        try:
            r = requests.get(f"{CAL_API}/calendars/primary/events",
                             headers={"Authorization": "Bearer " + token},
                             params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in _RETRYABLE:
                last_err = RuntimeError(f"events.list {r.status_code}")
            else:
                raise RuntimeError(f"events.list {r.status_code}: {r.text[:150]}")
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
        if i + 1 < attempts:
            time.sleep(1 + i)  # bounded linear backoff (1s, 2s)
    raise last_err or RuntimeError("events.list failed")


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
        d = _get_events_page(token, params)
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
    # Fetch specific contact emails BDR-booked in #conference-meetings once per cycle
    bdr_contacts = fetch_bdr_contacts(slack_token, start, end) if slack_token else set()
    if bdr_contacts:
        print(f"[blitz] {len(bdr_contacts)} BDR-booked contact(s) excluded from AE credit", flush=True)
    for ae in aes:
        try:
            evs = list_events(_token(sa_info, ae), umin)
            out[ae] = count_bookings(evs, ae, start, end, bdr_contacts)
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


def _is_board_message(m, me):
    """True if `m` is one of the bot's own leaderboard posts."""
    assert isinstance(m, dict), "m must be a dict"
    if not ((me and m.get("user") == me) or m.get("bot_id")):
        return False
    t = m.get("text", "") or ""
    return ("booked as a team" in t) or (BLITZ_TITLE and BLITZ_TITLE in t)


def delete_prior_boards(client):
    """Delete EVERY existing bot leaderboard message in the channel.

    Called on boot so a restart (deploy or Railway auto-restart) can never leave
    a duplicate pinned board behind. Bounded to the recent history window.
    """
    try:
        me = (client.auth_test() or {}).get("user_id")
    except Exception:
        me = None
    try:
        hist = client.conversations_history(channel=BLITZ_CHANNEL_ID, limit=50)
    except Exception as e:
        print(f"[blitz] history read for cleanup failed: {e}", flush=True)
        return 0
    n = 0
    for m in hist.get("messages", []):  # bounded (<=50)
        if _is_board_message(m, me):
            try:
                client.chat_delete(channel=BLITZ_CHANNEL_ID, ts=m["ts"])
                n += 1
            except Exception:
                pass
    return n


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


def process_counts(client, counts, last):
    """Apply a fresh poll to `last` (mutated in place) and return board rows.

    Fires a hype message for each genuinely new booking. `last` is seeded from
    persisted state on boot, so a restart never re-fires hype for bookings that
    were already celebrated. A None count (read failed this cycle) keeps the
    AE's last-known number rather than dropping them to 0.
    """
    assert isinstance(counts, dict), "counts must be a dict"
    assert isinstance(last, dict), "last must be a dict"
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
    return rows


def _persist_counts(last):
    """Save per-AE counts into the state file so hype survives a restart.
    Loads first so board_ts/sig written by refresh_board are preserved."""
    assert isinstance(last, dict), "last must be a dict"
    st = _load_state()
    st["counts"] = last
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
    # Restore counts from disk so a restart doesn't re-spam hype for meetings
    # that were already celebrated.
    last = {k: int(v) for k, v in (_load_state().get("counts") or {}).items()}
    print(f"[blitz] calendar poller: {len(BLITZ_AES)} AEs, every {BLITZ_POLL_SECS}s, "
          f"window {start.date()}..{end.date()}", flush=True)
    # Force a fresh board on boot: first delete EVERY existing board (so a
    # restart never leaves duplicate pinned boards), then clear the saved
    # pointer and post exactly one.
    try:
        removed = delete_prior_boards(client)
        st = _load_state()
        st.pop("board_ts", None)
        st.pop("sig", None)
        _save_state(st)
        refresh_board(client, [(display_name(ae), last.get(ae, 0)) for ae in BLITZ_AES])
        posted = _load_state().get("board_ts")
        print(f"[blitz] initial board posted (ts={posted}, cleaned {removed} old)" if posted
              else "[blitz] initial board NOT posted (post returned no ts)", flush=True)
    except Exception as e:
        print(f"[blitz] initial board post failed: {e}", flush=True)
    while True:  # bounded by process lifetime; each cycle isolated
        try:
            counts = poll_once(sa_info, BLITZ_AES, start, end, slack_token=client.token)
            rows = process_counts(client, counts, last)
            _persist_counts(last)  # before refresh_board, which preserves the key
            refresh_board(client, rows)
        except Exception as e:
            print(f"[blitz] poller cycle error: {e}", flush=True)
        time.sleep(BLITZ_POLL_SECS)
