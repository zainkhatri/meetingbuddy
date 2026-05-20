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
}

# HubSpot owner id → display name (matches values in Ellen's "Meeting Sourced By" column)
OWNER_DISPLAY = {
    '88760040': 'Zain',
    '162210484': 'Jacob',
    '82377567': 'Dani',
}

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


def _find_row(rows, company, date):
    """Locate existing row matching (Prospect Company, Meeting Date), case-insensitive.

    rows: list of row lists (1-indexed in sheet; rows[0] is header).
    Returns 1-based row number in sheet, or None.
    """
    if not (company and date and _headers):
        return None
    co_col = _headers.get('Prospect Company')
    dt_col = _headers.get('Meeting Date')
    if not (co_col and dt_col):
        return None
    target_co = company.strip().lower()
    target_dt = date.strip()
    for i in range(1, len(rows)):  # skip header at index 0
        row = rows[i]
        if len(row) < max(co_col, dt_col):
            continue
        if row[co_col - 1].strip().lower() == target_co and row[dt_col - 1].strip() == target_dt:
            return i + 1  # 1-based sheet row
    return None


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

    existing_row = _find_row(rows, payload['Prospect Company'], payload['Meeting Date'])

    # Build the value list in column order from the sheet's actual headers
    header_row = rows[0] if rows else []
    new_values = [payload.get(h.strip(), '') for h in header_row]

    if existing_row is None:
        try:
            _retry(ws.append_row, new_values, value_input_option='USER_ENTERED')
            return {'action': 'inserted', 'row': len(rows) + 1}
        except Exception as e:
            log.exception('sheet_sync append failed')
            return {'action': 'error', 'error': str(e)}

    # Update: preserve any non-empty existing cell when our payload value is empty
    current = rows[existing_row - 1] if len(rows) >= existing_row else []
    merged = []
    for i, h in enumerate(header_row):
        new_val = payload.get(h.strip(), '')
        old_val = current[i] if i < len(current) else ''
        merged.append(new_val if new_val else old_val)

    try:
        end_col = _col_letter(len(header_row))
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
