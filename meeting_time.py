"""Turn a booking post's date + time-as-written + zone into a UTC start.

The parser used to ask the model for "HH:MM in UTC" next to the LOCAL date, and
callers glued them together — so anything at 5pm Pacific or later landed a day
early (USAA, ITC 2026), and "PST" in September was converted as UTC-8 instead of
PDT. The model now reports what the rep wrote; the zone math happens here."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"   # reps write PST/PT for Pacific year-round

_ZONES = {
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "bst": "Europe/London", "uk": "Europe/London", "london": "Europe/London",
}


def _zone(label) -> ZoneInfo:
    key = (label or "").strip().lower().replace(".", "")
    return ZoneInfo(_ZONES.get(key, DEFAULT_TZ))


def start_utc_iso(date_str, hhmm, tz_label) -> str | None:
    """'2026-09-30', '17:00', 'PST' → '2026-10-01T00:00:00Z'. None on bad input."""
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
