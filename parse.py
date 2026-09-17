"""Parse blitz-channel messages into intents.

Intents:
  ("book", n)        -> credit n booked meetings to the poster (n>=1)
  ("undo", None)     -> remove the poster's last booking
  ("standings", None)-> post the current leaderboard on demand
  None               -> ignore (ordinary chatter)

Design: a booking is an explicit, unambiguous post that starts with the word
"booked" (reps already say this when they land one). An optional multiplier
("booked x3", "booked +2") logs several at once. This keeps AE effort to a
single celebratory line and avoids counting normal conversation.
"""

import re

from store import MAX_BOOKINGS_PER_MSG

_BOOK_RE = re.compile(r"^\s*booked\b", re.IGNORECASE)
_MULT_RE = re.compile(r"(?:x|\+)\s*(\d+)", re.IGNORECASE)
_STANDINGS_RE = re.compile(r"^\s*(standings|leaderboard|board)\s*$", re.IGNORECASE)
_UNDO_RE = re.compile(r"^\s*undo\b", re.IGNORECASE)


def parse_message(text):
    """Return an intent tuple for a raw Slack message, or None to ignore it.

    Simple/standalone path: a booking is a line starting with "booked". The
    meetingbuddy host instead uses command_of() + its own Claude parser for the
    rich BDR-style booking block.
    """
    assert text is None or isinstance(text, str), "text must be str or None"
    if not text:
        return None
    if _STANDINGS_RE.match(text):
        return ("standings", None)
    if _UNDO_RE.match(text):
        return ("undo", None)
    if _BOOK_RE.match(text):
        n = 1
        m = _MULT_RE.search(text)
        if m:
            n = max(1, min(MAX_BOOKINGS_PER_MSG, int(m.group(1))))
        assert n >= 1, "booking count must be positive"
        return ("book", n)
    return None


def command_of(text):
    """Return 'undo' or 'standings' if the message is that command, else None.

    Used by the meetingbuddy host, where booking *content* is parsed by Claude;
    only the two control words are matched here.
    """
    assert text is None or isinstance(text, str), "text must be str or None"
    if not text:
        return None
    if _STANDINGS_RE.match(text):
        return "standings"
    if _UNDO_RE.match(text):
        return "undo"
    return None


def looks_like_booking(text):
    """Cheap prefilter so ordinary chatter never hits the Claude parser."""
    assert text is None or isinstance(text, str), "text must be str or None"
    if not text:
        return False
    low = text.lower()
    return ("book" in low) or ("@" in text and "meeting" in low)
