"""Push HubSpot meetings into Ellen's "Full Meeting Tracker" tab.

Single public function: upsert_meeting_row(payload). Match key is
(Prospect Company, Meeting Date). Existing rows are updated in place;
never blank a non-empty cell with an empty value (BDR-added context
in Notes/AE/Location/Follow-Up is preserved).

Configured via env (reuses conference_buddy's OAuth token):
  GOOGLE_SHEETS_TOKEN          path to OAuth token JSON on disk
  GOOGLE_SHEETS_TOKEN_JSON     (Railway) raw JSON, materialized to GOOGLE_SHEETS_TOKEN at boot
  ELLEN_SHEET_ID               spreadsheet id
  ELLEN_TAB_NAME               worksheet/tab name (default: "Full Meeting Tracker")

Failure mode: log + return {'action': 'error', ...}. Never raises.
The caller (meeting_bot.py) treats sheet sync as best-effort safe net;
HubSpot is the source of truth.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

SHEET_ID = os.environ.get('ELLEN_SHEET_ID')
TAB_NAME = os.environ.get('ELLEN_TAB_NAME', 'Full Meeting Tracker')
TOKEN_PATH = os.environ.get('GOOGLE_SHEETS_TOKEN')
TOKEN_JSON = os.environ.get('GOOGLE_SHEETS_TOKEN_JSON')  # Railway: materialized to TOKEN_PATH

# Conference slug → display name in Ellen's sheet
CONFERENCE_DISPLAY = {
    'wsia_uw_summit': 'WSIA Underwriting Summit',
    'wsia_dinner': 'WSIA Dinner',
    'insurtech_ny_spring': 'InsurTech New York Spring Conference',
    'insurtech_insights': 'InsurTech Insights USA',
    'iiusa': 'InsurTech Insights USA',
    'insurance_innovators': 'Insurance Innovators Nashville',
    'tmpaa': 'Target Markets Mid Year Meeting',
    'tmpcc': 'Target Markets Mid Year Meeting',
    'target_markets_midyear': 'Target Markets Mid Year Meeting',
    'rims_riskworld': 'RIMS Riskworld Conference 2026',
    'nashville_dinner': 'Nashville Dinner',
    'ny_dinner': 'New York Dinner',
    'insurance_insider': 'Insurance Insider 2026',
    'reuters_es': 'Reuters - The Insurer E&S',
    'reuters_program_manager': 'Reuters - The Insurer Program Manager',
    'future_of_insurance': 'Future of Insurance USA (Reuters)',
}

# HubSpot owner id → display name (matches values in Ellen's "Meeting Sourced By" column)
OWNER_DISPLAY = {
    '88760040': 'Zain',
    '162210484': 'Jacob',
    '82377567': 'Dani',
    '164943105': 'Ben',
    '92184259': 'Matt',
}


def bdr_sdr_owner_value(owner_id):
    """Map a HubSpot owner id to the `sdr_owner` enum display value.

    Returns '' for non-BDR owners or blank input, so callers can safely skip
    writing the property. The roster is OWNER_DISPLAY (the same map used for the
    sheet's 'Meeting Sourced By' column), so BDRs are defined in exactly one place.
    """
    return OWNER_DISPLAY.get(str(owner_id or ''), '')

# Sheet column headers, in expected order. Header row is read at runtime,
# so reordering in the sheet is fine — these are the names we look for.
SHEET_COLUMNS = [
    'Conference',
    'Meeting Sourced By',
    'Account Executive',
    'FurtherAI Rep in Meeting',
    'Meeting Date',
    'Meeting Time',
    'Meeting Location (Booth, Dinner, Coffee, etc.)',
    'Prospect Company',
    'Prospect Name',
    'Prospect Title',
    'Prospect Email',
    'Meeting Status',
    'Notes',
    'Follow-Up Demo Scheduled?',
]

STATUS_MAP = {
    'SCHEDULED': 'Scheduled',
    'COMPLETED': 'Completed',
    'CANCELED': 'CANCELLED',
    'CANCELLED': 'CANCELLED',
    'NO_SHOW': 'No Show',
    'RESCHEDULED': 'Rescheduled',
}

_client_lock = threading.Lock()
_ws = None  # cached worksheet handle
_headers = None  # cached header row mapping (name → 1-based col index)


def _materialize_token():
    """If running on Railway with GOOGLE_SHEETS_TOKEN_JSON, write it to GOOGLE_SHEETS_TOKEN path."""
    if not (TOKEN_PATH and TOKEN_JSON):
        return
    from pathlib import Path
    p = Path(TOKEN_PATH)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TOKEN_JSON)


def _connect():
    """Build (or return cached) gspread worksheet handle. None if unconfigured."""
    global _ws, _headers
    if _ws is not None:
        return _ws
    with _client_lock:
        if _ws is not None:
            return _ws
        if not (SHEET_ID and TOKEN_PATH):
            log.warning('sheet_sync unconfigured (missing ELLEN_SHEET_ID or GOOGLE_SHEETS_TOKEN)')
            return None
        try:
            _materialize_token()
            import gspread
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            with open(TOKEN_PATH) as f:
                tok = json.load(f)
            creds = Credentials(
                token=tok['token'], refresh_token=tok['refresh_token'],
                token_uri=tok['token_uri'], client_id=tok['client_id'],
                client_secret=tok['client_secret'], scopes=tok['scopes'],
            )
            if creds.expired or not creds.valid:
                creds.refresh(Request())
                tok['token'] = creds.token
                with open(TOKEN_PATH, 'w') as f:
                    json.dump(tok, f)
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(SHEET_ID)
            _ws = sh.worksheet(TAB_NAME)
            header_row = _ws.row_values(1)
            _headers = {name.strip(): i + 1 for i, name in enumerate(header_row) if name.strip()}
            log.info('sheet_sync connected: tab=%s headers=%d', TAB_NAME, len(_headers))
            return _ws
        except Exception:
            log.exception('sheet_sync connect failed')
            return None


def _retry(fn, *args, **kwargs):
    """Run fn with up to 3 attempts on transient errors."""
    last_exc = None
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            transient = '429' in msg or '500' in msg or '502' in msg or '503' in msg or 'timeout' in msg
            if not transient or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last_exc


def _to_dt(value):
    """Accept ms epoch (int/str) OR ISO string ('2026-05-04T14:00:00Z'). Returns UTC datetime or None."""
    if value is None or value == '':
        return None
    s = str(value)
    try:
        if s.isdigit():
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def _fmt_date(value):
    """HubSpot meeting start → 'M/D' in ET."""
    dt = _to_dt(value)
    if not dt:
        return ''
    et = dt.astimezone(timezone(timedelta(hours=-4)))
    return f'{et.month}/{et.day}'


def _fmt_time(value):
    """HubSpot meeting start → 'H:MM AM/PM ET'."""
    dt = _to_dt(value)
    if not dt:
        return ''
    et = dt.astimezone(timezone(timedelta(hours=-4)))
    return et.strftime('%-I:%M %p ET')


_KNOWN_CONFERENCES = set(CONFERENCE_DISPLAY.keys())

# Date-based fallback for conferences the bot doesn't auto-tag yet. Used when
# HubSpot has conference_source='other' but the meeting date matches a known event.
# (M, D) → slug
_DATE_FALLBACK = {
    (5, 20): 'reuters_es',
    (5, 21): 'reuters_program_manager',
}


def _infer_slug_from_date(meeting_start_ms):
    dt = _to_dt(meeting_start_ms)
    if not dt:
        return None
    et = dt.astimezone(timezone(timedelta(hours=-4)))
    return _DATE_FALLBACK.get((et.month, et.day))


def build_payload(*, conference_slug, sourced_by_owner_id, meeting_start_ms,
                  company, contact_first, contact_last, contact_title,
                  contact_email, hs_meeting_outcome):
    """Translate HubSpot fields into the sheet's column dict.

    Returns dict keyed by sheet column name. Missing/unknown fields → ''.
    Caller is responsible for only invoking this when conference_slug is non-empty.
    """
    name = ' '.join(filter(None, [(contact_first or '').strip(), (contact_last or '').strip()]))
    # Unknown/"other" conference: try inferring from the meeting date
    if conference_slug not in _KNOWN_CONFERENCES:
        conference_slug = _infer_slug_from_date(meeting_start_ms)
    display = CONFERENCE_DISPLAY.get(conference_slug, '') if conference_slug in _KNOWN_CONFERENCES else ''
    return {
        'Conference': display,
        'Meeting Sourced By': OWNER_DISPLAY.get(str(sourced_by_owner_id or ''), ''),
        'Account Executive': '',
        'FurtherAI Rep in Meeting': '',
        'Meeting Date': _fmt_date(meeting_start_ms),
        'Meeting Time': _fmt_time(meeting_start_ms),
        'Meeting Location (Booth, Dinner, Coffee, etc.)': '',
        'Prospect Company': (company or '').strip(),
        'Prospect Name': name,
        'Prospect Title': (contact_title or '').strip(),
        'Prospect Email': (contact_email or '').strip(),
        'Meeting Status': STATUS_MAP.get((hs_meeting_outcome or '').upper(), ''),
        'Notes': '',
        'Follow-Up Demo Scheduled?': '',
    }


# Company-name suffixes stripped before matching, so "Truckers Insurance Associates"
# matches "Truckers Insurance" and "SiriusPoint" matches "Sirius Point".
_COMPANY_SUFFIXES = (
    ' associates', ' companies', ' holdings limited', ' holdings',
    ' insurance group', ' insurance services', ' insurance company', ' insurance',
    ' group', ' corp', ' corporation', ' inc', ' llc', ' ltd', ' limited',
    ' co', ' company',
)


def _norm_company(name):
    s = (name or '').strip().lower().rstrip('.').rstrip(',').strip()
    # Strip trailing suffixes iteratively so "Truckers Insurance Associates"
    # and "Truckers Insurance" both collapse to "truckers"
    changed = True
    while changed:
        changed = False
        for suf in _COMPANY_SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                changed = True
                break
    # Drop all whitespace + punctuation for final compare ("Sirius Point" == "SiriusPoint")
    return ''.join(ch for ch in s if ch.isalnum())


def _company_date_matches(rows, company, date):
    """All 1-based sheet row numbers matching (Prospect Company, Meeting Date).
    Company is normalized (case/space/suffix-insensitive); date is strict."""
    if not (company and date and _headers):
        return []
    co_col = _headers.get('Prospect Company')
    dt_col = _headers.get('Meeting Date')
    if not (co_col and dt_col):
        return []
    target_co = _norm_company(company)
    target_dt = date.strip()
    matches = []
    for i in range(1, len(rows)):
        row = rows[i]
        if len(row) < max(co_col, dt_col):
            continue
        if _norm_company(row[co_col - 1]) == target_co and row[dt_col - 1].strip() == target_dt:
            matches.append(i + 1)
    return matches


def _find_row(rows, company, date, contact_name=None, meeting_time=None):
    """Locate the existing row for this meeting.

    Primary match: (Prospect Company, Meeting Date) — company normalized
    (case/space/suffix-insensitive), date strict. If several rows share that
    (company, date) (multi-contact meeting), disambiguate by contact name.

    Fallback match: (Prospect Name, Meeting Date) when the primary finds
    nothing. The SAME prospect on the SAME date is the same meeting even if the
    company string differs between syncs (e.g. 'Ironshore Insurance' vs
    'Liberty Mutual', 'Allied American USA' vs 'Allied American') — without this
    the bot appends a near-duplicate. A time guard keeps two genuinely separate
    same-day meetings for one person apart: we only treat it as the same row
    when the times match or either side's time is blank.

    rows: list of row lists (1-indexed in sheet; rows[0] is header).
    Returns 1-based row number in sheet, or None.
    """
    name_col = _headers.get('Prospect Name')
    tm_col = _headers.get('Meeting Time')
    target_name = (contact_name or '').strip().lower()
    target_tm = (meeting_time or '').strip().lower()

    def _time_ok(row):
        # Same meeting unless both times are present and differ.
        if not (tm_col and target_tm and len(row) >= tm_col):
            return True
        existing_tm = row[tm_col - 1].strip().lower()
        return (not existing_tm) or existing_tm == target_tm

    matches = _company_date_matches(rows, company, date)
    # A lone (company, date) match is almost certainly the same meeting — return
    # it time-agnostically, so a human reformatting the time cell can't make the
    # daily reconcile append a duplicate.
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Several rows share (company, date): pick the one whose name AND time
        # agree, so the reconcile lands on the right slot instead of the first.
        if target_name and name_col:
            for row_num in matches:
                row = rows[row_num - 1]
                if row[name_col - 1].strip().lower() == target_name and _time_ok(row):
                    return row_num
        # no name/time-compatible pick → fall through to name+date fallback

    # Fallback: same prospect name + same date (company string may differ).
    if target_name and name_col:
        dt_col = _headers.get('Meeting Date')
        target_dt = (date or '').strip()
        if dt_col:
            for i in range(1, len(rows)):
                row = rows[i]
                if len(row) < max(name_col, dt_col):
                    continue
                if row[name_col - 1].strip().lower() != target_name:
                    continue
                if row[dt_col - 1].strip() != target_dt:
                    continue
                if not _time_ok(row):  # differing non-blank times = different meeting
                    continue
                return i + 1
    return None  # genuinely new meeting → append


def upsert_meeting_row(payload):
    """Insert or update one row in Ellen's sheet.

    payload: dict from build_payload().
    Returns {'action': 'inserted'|'updated'|'skipped'|'error', ...}.
    Never raises.
    """
    if not payload.get('Conference') or not payload.get('Prospect Company'):
        return {'action': 'skipped', 'reason': 'missing conference or company'}

    ws = _connect()
    if ws is None:
        return {'action': 'skipped', 'reason': 'unconfigured'}

    try:
        rows = _retry(ws.get_all_values)
    except Exception as e:
        log.exception('sheet_sync read failed')
        return {'action': 'error', 'error': str(e)}

    existing_row = _find_row(rows, payload['Prospect Company'], payload['Meeting Date'],
                              contact_name=payload.get('Prospect Name'),
                              meeting_time=payload.get('Meeting Time'))

    # Guard against flooding: a payload with no Prospect Name can never match a
    # specific row when several share the same (company, date), so _find_row
    # returns None and we'd append a fresh blank-name row on every reconcile.
    # If that (company, date) is already represented, the contactless row adds
    # nothing — skip it rather than pile up duplicates.
    if existing_row is None and not (payload.get('Prospect Name') or '').strip():
        if _company_date_matches(rows, payload['Prospect Company'], payload['Meeting Date']):
            return {'action': 'skipped', 'reason': 'contactless duplicate of existing company/date'}

    # Build the value list in column order from the sheet's actual headers.
    header_row = rows[0] if rows else []
    # COLUMN-WIDTH CLAMP — never write past the last *named* header column.
    # get_all_values() pads header_row to the sheet's used width, so any phantom
    # trailing columns (e.g. a stray wide paste, or the old +4-drift bug that
    # ratcheted this tab out to 5825 columns) would otherwise make new_values
    # that wide and re-expand the grid on every write. Clamping to the real
    # header extent means the bot physically cannot widen the sheet again.
    last_col = max((i + 1 for i, h in enumerate(header_row) if h.strip()),
                   default=len(header_row))
    new_values = [payload.get(h.strip(), '') for h in header_row][:last_col]

    if existing_row is None:
        try:
            # table_range='A1' is REQUIRED: without it gspread lets the Sheets
            # API auto-detect the table, which intermittently starts the append
            # at a non-A column (observed: column E, a +4 shift). Shifted rows
            # land company/name/date in the wrong columns, so _find_row can
            # never match them again and every reconcile re-appends a fresh
            # copy — the unbounded flood. Pinning to A1 forces column-A writes.
            _retry(ws.append_row, new_values, value_input_option='USER_ENTERED', table_range='A1')
            return {'action': 'inserted', 'row': len(rows) + 1}
        except Exception as e:
            log.exception('sheet_sync append failed')
            return {'action': 'error', 'error': str(e)}

    # Update: bot can only FILL empty cells, never overwrite BDR-curated content.
    # If the existing cell has any value, keep it. Only write to empty cells.
    current = rows[existing_row - 1] if len(rows) >= existing_row else []
    merged = []
    any_change = False
    for i, h in enumerate(header_row[:last_col]):  # clamped to named columns
        new_val = payload.get(h.strip(), '')
        old_val = current[i] if i < len(current) else ''
        if old_val.strip():
            merged.append(old_val)
        else:
            merged.append(new_val)
            if new_val:
                any_change = True
    if not any_change:
        return {'action': 'updated', 'row': existing_row, 'noop': True}

    try:
        end_col = _col_letter(last_col)
        rng = f'A{existing_row}:{end_col}{existing_row}'
        _retry(ws.update, rng, [merged], value_input_option='USER_ENTERED')
        return {'action': 'updated', 'row': existing_row}
    except Exception as e:
        log.exception('sheet_sync update failed')
        return {'action': 'error', 'error': str(e)}


def _col_letter(n):
    """1 -> A, 27 -> AA, ..."""
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
