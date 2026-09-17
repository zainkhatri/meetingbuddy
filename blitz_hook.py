"""ITC Exec Blitz leaderboard — a self-contained hook hosted inside meetingbuddy.

Isolated from the booking pipeline: `handle_message` diverts blitz-channel
messages here and returns, and the caller wraps this in try/except so nothing
here can ever affect meetingbuddy's HubSpot/booking flow. Entirely inert unless
BLITZ_CHANNEL_ID is set.

Outputs only: an AE posts a booking in the blitz channel using the same
BDR-style block the team already uses ("ITC MEETING BOOKED! / name - persona @
company / ..."). meetingbuddy's own Claude parser reads the fields; we credit
the poster one point per booking and refresh a pinned live leaderboard. Count
only, no HubSpot writes.

Env:
  BLITZ_CHANNEL_ID  channel to watch (unset -> hook never runs)
  BLITZ_TITLE       leaderboard header (optional)
  BLITZ_STATE_PATH  JSON tally path; default /data or ./blitz_state.json
"""

import os
import threading

import store
from leaderboard import render_blocks, render_text
from parse import command_of, looks_like_booking

BLITZ_CHANNEL_ID = os.environ.get('BLITZ_CHANNEL_ID')
BLITZ_TITLE = os.environ.get('BLITZ_TITLE', 'AE Blitz: Booked Meetings')


def _default_state_path():
    return '/data/blitz_state.json' if os.path.isdir('/data') else 'blitz_state.json'


STATE_PATH = os.environ.get('BLITZ_STATE_PATH', _default_state_path())

_lock = threading.Lock()
_state = store.load(STATE_PATH)
_name_cache = {}


def _display_name(client, user_id):
    assert isinstance(user_id, str) and user_id, 'user_id must be a non-empty string'
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = user_id
    try:
        info = (client.users_info(user=user_id).get('user') or {})
        prof = info.get('profile') or {}
        name = prof.get('display_name') or prof.get('real_name') or info.get('real_name') or user_id
    except Exception:
        name = user_id
    _name_cache[user_id] = name
    return name


def _refresh_board(client):
    """Repost the leaderboard at the bottom of the channel. Caller holds _lock.

    We delete the previous board and post a fresh one so there is always exactly
    ONE leaderboard and it sits at the bottom (where Slack lands you on open) —
    no message pile-up. Deleting the bot's own message uses chat:write.
    """
    rows = store.standings(_state)
    total = store.total_booked(_state)
    last = _state.get('last')
    blocks = render_blocks(rows, BLITZ_TITLE, total, last)
    text = render_text(rows, BLITZ_TITLE, total, last)
    old_ts = _state.get('board_ts')
    if old_ts:
        try:
            client.chat_delete(channel=BLITZ_CHANNEL_ID, ts=old_ts)
        except Exception:
            pass  # already gone or racing; the repost below is what matters
    _state['board_ts'] = None
    try:
        resp = client.chat_postMessage(channel=BLITZ_CHANNEL_ID, blocks=blocks, text=text)
        _state['board_ts'] = resp['ts']
        try:
            client.pins_add(channel=BLITZ_CHANNEL_ID, timestamp=resp['ts'])
        except Exception:
            pass
    except Exception:
        pass


def _react(client, ts, name):
    try:
        client.reactions_add(channel=BLITZ_CHANNEL_ID, timestamp=ts, name=name)
    except Exception:
        pass


def _booking_detail(b):
    """One-line "Name, Title @ Company" from a parsed booking dict."""
    name = f"{b.get('contact_first_name', '') or ''} {b.get('contact_last_name', '') or ''}".strip()
    title = (b.get('contact_title') or '').strip()
    company = (b.get('company_name') or '').strip()
    out = name or 'Prospect'
    if title:
        out += f", {title}"
    if company:
        out += f" @ {company}"
    return out


def handle(event, client, parse_fn):
    """Process one blitz-channel message. Returns True if it did something.

    parse_fn is meetingbuddy's parse_with_claude: text -> dict | [dict] | None,
    each dict carrying is_booking + contact/company fields.
    """
    assert BLITZ_CHANNEL_ID, 'handle called without BLITZ_CHANNEL_ID'
    assert callable(parse_fn), 'parse_fn must be callable'
    if event.get('subtype') or event.get('bot_id'):
        return False  # ignore edits, joins, bot posts
    user_id = event.get('user')
    ts = event.get('ts')
    text = event.get('text', '')
    if not user_id or not ts:
        return False

    cmd = command_of(text)
    if cmd == 'standings':
        with _lock:
            _refresh_board(client)
        return True
    if cmd == 'undo':
        with _lock:
            new_total = store.undo_last(_state, user_id)
            store.save(STATE_PATH, _state)
            _react(client, ts, 'leftwards_arrow_with_hook' if new_total is not None else 'shrug')
            _refresh_board(client)
        return True

    with _lock:
        if ts in _state.get('processed_ts', []):
            return False  # already counted (live handler or a prior replay/sweep pass)

    if not looks_like_booking(text):
        return False  # ordinary chatter never hits the Claude parser
    parsed = parse_fn(text)
    if isinstance(parsed, dict) and parsed.get('_parse_error'):
        _react(client, ts, 'question')  # couldn't read it; flag rather than drop
        return False
    bookings = parsed if isinstance(parsed, list) else [parsed]
    bookings = [b for b in bookings if b and b.get('is_booking')]
    if not bookings:
        return False

    delta = min(len(bookings), store.MAX_BOOKINGS_PER_MSG)
    detail = _booking_detail(bookings[0])
    if len(bookings) > 1:
        detail += f" (+{len(bookings) - 1} more)"
    with _lock:
        total = store.add_booking(_state, user_id, _display_name(client, user_id), ts, delta, detail)
        if total is not None:
            store.save(STATE_PATH, _state)
            _react(client, ts, 'white_check_mark')
            _refresh_board(client)
    return True


def post_initial_board(client):
    """Post the opening leaderboard once at startup (optional)."""
    if not BLITZ_CHANNEL_ID:
        return
    with _lock:
        _refresh_board(client)
