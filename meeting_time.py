"""Turn a booking post's date + time-as-written + zone into a UTC start.

The parser used to ask the model for "HH:MM in UTC" next to the LOCAL date, and
callers glued them together — so anything at 5pm Pacific or later landed a day
early (USAA, ITC 2026), and "PST" in September was converted as UTC-8 instead of
PDT. The model now reports what the rep wrote; the zone math happens here."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Los_Angeles"   # reps write PST/PT for Pacific year-round

_ZONES = {
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    # "MST" is literally UTC-7 all year (Phoenix) — the same wall clock as Vegas in
    # summer, which is what reps at ITC mean. MT/MDT/Mountain follow Denver's DST.
    "mt": "America/Denver", "mst": "America/Phoenix", "mdt": "America/Denver", "mountain": "America/Denver",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "bst": "Europe/London", "uk": "Europe/London", "london": "Europe/London",
}


def _zone(label) -> ZoneInfo:
    """Zone for a label as written: exact key, an IANA name, or the first known
    token inside it ("Eastern Time", "EDT (NYC)", "10am ET / 7am PT" → ET)."""
    raw = (label or "").strip()
    key = raw.lower().replace(".", "")
    if key in _ZONES:
        return ZoneInfo(_ZONES[key])
    if "/" in raw and " " not in raw:
        try:
            return ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    for tok in re.findall(r"[a-z]+", key):
        if tok in _ZONES:
            return ZoneInfo(_ZONES[tok])
    if raw:
        print(f"[meeting_time] unknown zone {raw!r} — assuming Pacific", flush=True)
    return ZoneInfo(DEFAULT_TZ)


def _hhmm(text) -> str | None:
    """'9:30' / '17:00' / '5:00 PM' / '12:15 am' → 'HH:MM' (24h), else None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?\s*", str(text or ""), re.IGNORECASE)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def start_utc_iso(date_str, hhmm, tz_label) -> str | None:
    """'2026-09-30', '17:00', 'PST' → '2026-10-01T00:00:00Z'. None on bad input."""
    hhmm = _hhmm(hhmm)
    if not (date_str and hhmm):
        return None
    try:
        local = datetime.fromisoformat(f"{date_str}T{hhmm}:00").replace(tzinfo=_zone(tz_label))
    except ValueError:
        return None
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_booking(b: dict) -> dict:
    """Set b['meeting_start_utc'] (and keep meeting_time_utc in step with it).
    Prefers the local time + zone; falls back to a bare UTC time for older
    parses. meeting_date stays the LOCAL date — it's what reps see."""
    if not isinstance(b, dict):
        return b
    date_str = b.get("meeting_date")
    start = start_utc_iso(date_str, b.get("meeting_time_local"), b.get("meeting_tz"))
    if start is None and b.get("meeting_time_utc"):
        start = start_utc_iso(date_str, b.get("meeting_time_utc"), "UTC")
    b["meeting_start_utc"] = start
    if start:
        b["meeting_time_utc"] = start[11:16]
    return b


def start_datetime(b: dict, fallback_date=None, fallback_hhmm_utc="14:00"):
    """UTC datetime for a parsed booking: meeting_start_utc if set, else the
    old date + default-hour behaviour. None when there's no date at all."""
    start = (b or {}).get("meeting_start_utc")
    if start:
        return datetime.fromisoformat(start.replace("Z", "+00:00"))
    date_str = (b or {}).get("meeting_date") or fallback_date
    if not date_str:
        return None
    iso = start_utc_iso(date_str, (b or {}).get("meeting_time_utc") or fallback_hhmm_utc, "UTC")
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def reference_block(post_ts=None, days: int = 14) -> str:
    """Prompt context anchoring relative dates to the POST's day in Pacific time,
    with a weekday table — the model resolved "Wednesday" from a Tuesday post to
    Thursday, and the old reference used the UTC date (a day ahead after 5pm PT)."""
    base = datetime.fromtimestamp(float(post_ts), ZoneInfo(DEFAULT_TZ)) if post_ts \
        else datetime.now(ZoneInfo(DEFAULT_TZ))
    d0 = base.date()
    table = ", ".join(f"{(d0 + timedelta(days=i)):%a %Y-%m-%d}" for i in range(days))
    return (f"[Post date: {d0:%a %Y-%m-%d} (Pacific). Resolve weekday names and "
            f"'today'/'tomorrow' from the post date using this calendar: {table}. "
            f"A weekday means its NEXT occurrence on or after the post date. Dates "
            f"without a year → the year that puts the meeting after the post date.]")


def pt_date(value) -> str | None:
    """Pacific calendar date of a HubSpot start (ISO string or epoch ms)."""
    v = str(value or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromtimestamp(int(v) / 1000, timezone.utc) if v.isdigit() \
            else datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%d")
