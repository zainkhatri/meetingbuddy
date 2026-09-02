# tests/test_company_history.py
import types
import meeting_bot


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _install(monkeypatch, *, meetings=None, deals=None, contacts=None, company=None, owner=None):
    """Route HubSpot reads by URL. Missing kwargs -> 500 (simulates a failing sub-read)."""
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'meetings/search' in url:
            return _Resp(200, meetings) if meetings is not None else _Resp(500, {})
        if 'deals/search' in url:
            return _Resp(200, deals) if deals is not None else _Resp(500, {})
        if 'contacts/search' in url:
            return _Resp(200, contacts) if contacts is not None else _Resp(500, {})
        raise AssertionError(f'unexpected POST {url}')

    def fake_get(url, headers=None, params=None, timeout=None):
        if '/objects/companies/' in url:
            return _Resp(200, company) if company is not None else _Resp(500, {})
        if '/owners/' in url:
            return _Resp(200, owner) if owner is not None else _Resp(500, {})
        raise AssertionError(f'unexpected GET {url}')

    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    monkeypatch.setattr(meeting_bot.requests, 'get', fake_get)


def test_full_history(monkeypatch):
    _install(
        monkeypatch,
        meetings={'total': 3, 'results': [{'properties': {'hs_meeting_start_time': '2026-06-14T10:00:00Z'}}]},
        deals={'total': 1, 'results': [{'properties': {'dealname': 'Acme - Intro Calls',
                                                       'dealstage': 'appointmentscheduled', 'amount': '40000'}}]},
        contacts={'total': 5, 'results': []},
        company={'properties': {'hubspot_owner_id': '162210484', 'hs_last_activity_date': '2026-08-20T00:00:00Z'}},
    )
    h = meeting_bot.hs_company_history('C1')
    assert h['meetings_count'] == 3
    assert h['last_meeting_date'] == '2026-06-14'
    assert h['deal']['name'] == 'Acme - Intro Calls'
    assert h['deal']['open'] is True
    assert h['contacts_count'] == 5
    assert h['owner_name'] == 'jacob'          # roster lookup, no /owners/ GET needed
    assert h['last_activity_date'] == '2026-08-20'


def test_partial_degrades(monkeypatch):
    # only contacts read succeeds; meetings, deals, and company all fail (500)
    # -> helper still returns {'contacts_count': 2}
    _install(monkeypatch, contacts={'total': 2, 'results': []})
    h = meeting_bot.hs_company_history('C1')
    assert h == {'contacts_count': 2}


def test_all_empty_returns_none(monkeypatch):
    _install(monkeypatch,
             meetings={'total': 0, 'results': []},
             deals={'total': 0, 'results': []},
             contacts={'total': 0, 'results': []},
             company={'properties': {}})
    assert meeting_bot.hs_company_history('C1') is None


def test_comment_renders_history_block():
    parsed = {'company_name': 'Acme', 'segment': 'brokerage', 'company_size': 2000}
    history = {'meetings_count': 3, 'last_meeting_date': '2026-06-14',
               'deal': {'name': 'Acme - Intro Calls', 'stage': 'appointmentscheduled',
                        'amount': '40000', 'open': True},
               'contacts_count': 5, 'owner_name': 'jacob', 'last_activity_date': '2026-08-20'}
    out = meeting_bot._log_comment(parsed, False, poster='U1', history=history)
    assert 'spoken to' in out
    assert '3 prior meetings (last: 2026-06-14)' in out
    assert 'Acme - Intro Calls' in out
    assert '5 contacts on file' in out
    assert 'jacob' in out


def test_comment_unchanged_without_history():
    parsed = {'company_name': 'Acme', 'segment': 'brokerage', 'company_size': 2000}
    base = meeting_bot._log_comment(parsed, False, poster='U1')
    with_none = meeting_bot._log_comment(parsed, False, poster='U1', history=None)
    assert base == with_none
    assert 'spoken to' not in base
