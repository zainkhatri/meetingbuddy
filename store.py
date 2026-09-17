"""Tally store for the ITC blitz leaderboard.

Single source of truth is a JSON file on disk (Railway /data volume in prod,
local file in dev). One booked ITC meeting == one point, credited to the AE who
posted it. Idempotent on Slack message ts so restarts and event replays never
double-count. Supports undo and explicit multi-booking counts.

Power-of-Ten notes: no recursion, bounded loops, >=2 assertions per function,
all inputs validated, all return values checked by callers.
"""

import json
import os
import tempfile

MAX_BOOKINGS_PER_MSG = 20  # sanity bound on a single "booked xN" post


def new_state():
    """Return an empty, well-formed state dict."""
    return {"reps": {}, "events": [], "processed_ts": [], "board_ts": None, "last": None}


def load(path):
    """Load state from path, or a fresh state if the file is missing/corrupt."""
    assert isinstance(path, str) and path, "path must be a non-empty string"
    if not os.path.exists(path):
        return new_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return new_state()
    # Validate shape; fall back to a fresh state on anything unexpected.
    if not isinstance(data, dict) or "reps" not in data or "events" not in data:
        return new_state()
    data.setdefault("processed_ts", [])
    data.setdefault("board_ts", None)
    data.setdefault("last", None)
    assert isinstance(data["reps"], dict), "reps must be a dict"
    return data


def save(path, state):
    """Atomically write state to path."""
    assert isinstance(path, str) and path, "path must be a non-empty string"
    assert isinstance(state, dict) and "reps" in state, "state must be a state dict"
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _rep(state, user_id, name):
    """Return the mutable rep record for user_id, creating it if needed."""
    assert isinstance(user_id, str) and user_id, "user_id must be a non-empty string"
    assert isinstance(name, str), "name must be a string"
    rec = state["reps"].get(user_id)
    if rec is None:
        rec = {"name": name or user_id, "count": 0}
        state["reps"][user_id] = rec
    elif name:
        rec["name"] = name  # keep the freshest display name
    return rec


def add_booking(state, user_id, name, msg_ts, delta=1, detail=None):
    """Credit `delta` bookings to user_id, keyed on msg_ts for idempotency.

    `detail` is an optional human string ("Kari Thies, EVP @ Inszone") shown as
    the "latest booking" line on the board and kept in the event log for audit.
    Returns the new total for the rep, or None if this msg_ts was already
    counted (duplicate/replay).
    """
    assert isinstance(state, dict) and "reps" in state, "state must be a state dict"
    assert isinstance(msg_ts, str) and msg_ts, "msg_ts must be a non-empty string"
    n = int(delta)
    assert 1 <= n <= MAX_BOOKINGS_PER_MSG, "delta out of bounds"
    if msg_ts in state["processed_ts"]:
        return None
    rec = _rep(state, user_id, name)
    rec["count"] += n
    state["processed_ts"].append(msg_ts)
    state["events"].append({"ts": msg_ts, "user": user_id, "delta": n, "detail": detail or ""})
    if detail:
        state["last"] = {"detail": detail, "name": rec["name"]}
    return rec["count"]


def undo_last(state, user_id):
    """Remove the most recent positive booking event for user_id.

    Returns the rep's new total, or None if they have nothing to undo.
    """
    assert isinstance(state, dict) and "events" in state, "state must be a state dict"
    assert isinstance(user_id, str) and user_id, "user_id must be a non-empty string"
    for i in range(len(state["events"]) - 1, -1, -1):  # bounded reverse scan
        ev = state["events"][i]
        if ev["user"] == user_id and ev["delta"] > 0:
            rec = state["reps"].get(user_id)
            if rec is None:
                return None
            rec["count"] = max(0, rec["count"] - ev["delta"])
            state["events"].pop(i)
            if ev["ts"] in state["processed_ts"]:
                state["processed_ts"].remove(ev["ts"])
            return rec["count"]
    return None


def standings(state):
    """Return [(name, count), ...] sorted by count desc, then name asc."""
    assert isinstance(state, dict) and "reps" in state, "state must be a state dict"
    rows = [(r["name"], r["count"]) for r in state["reps"].values() if r["count"] > 0]
    rows.sort(key=lambda t: (-t[1], t[0].lower()))
    return rows


def total_booked(state):
    """Return the sum of all reps' booked meetings."""
    assert isinstance(state, dict) and "reps" in state, "state must be a state dict"
    return sum(r["count"] for r in state["reps"].values())
