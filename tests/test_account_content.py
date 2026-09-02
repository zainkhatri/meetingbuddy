# tests/test_account_content.py
import meeting_bot


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_search_objects_merges_id(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert 'meetings/search' in url
        assert json['sorts'][0]['propertyName'] == 'hs_meeting_start_time'
        assert json['limit'] == 8
        return _Resp(200, {'results': [
            {'id': 'M1', 'properties': {'hs_meeting_title': 'Intro', 'hs_meeting_start_time': '2026-06-14T10:00:00Z'}},
        ]})
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    out = meeting_bot._search_objects('meetings', 'C1',
        ['hs_meeting_title', 'hs_meeting_start_time'], 'hs_meeting_start_time', 8)
    assert out == [{'id': 'M1', 'hs_meeting_title': 'Intro', 'hs_meeting_start_time': '2026-06-14T10:00:00Z'}]


def test_search_objects_degrades(monkeypatch):
    monkeypatch.setattr(meeting_bot.requests, 'post', lambda *a, **k: _Resp(500, {}))
    assert meeting_bot._search_objects('notes', 'C1', ['hs_note_body'], 'hs_timestamp', 10) == []


def test_gather_account_content_three_sources(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'meetings/search' in url:
            return _Resp(200, {'results': [{'id': 'M1', 'properties': {'hs_meeting_title': 'Intro'}}]})
        if 'notes/search' in url:
            return _Resp(200, {'results': [{'id': 'N1', 'properties': {'hs_note_body': 'called'}}]})
        if 'emails/search' in url:
            return _Resp(200, {'results': [{'id': 'E1', 'properties': {'hs_email_subject': 'Re: pricing'}}]})
        raise AssertionError(url)
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    c = meeting_bot._gather_account_content('C1')
    assert c['meetings'][0]['id'] == 'M1'
    assert c['notes'][0]['hs_note_body'] == 'called'
    assert c['emails'][0]['hs_email_subject'] == 'Re: pricing'


def test_account_participants_resolves_names(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'associations/meetings/contacts/batch/read' in url:
            return _Resp(200, {'results': [{'from': {'id': 'M1'}, 'to': [{'toObjectId': 101}]}]})
        if 'associations/emails/contacts/batch/read' in url:
            return _Resp(200, {'results': [{'from': {'id': 'E1'}, 'to': [{'toObjectId': 102}]}]})
        if 'contacts/batch/read' in url:
            ids = {i['id'] for i in json['inputs']}
            assert ids == {'101', '102'}
            return _Resp(200, {'results': [
                {'id': '101', 'properties': {'firstname': 'Jane', 'lastname': 'Doe', 'jobtitle': 'VP Ops'}},
                {'id': '102', 'properties': {'firstname': 'Mark', 'lastname': 'Lee', 'jobtitle': None}},
            ]})
        raise AssertionError(url)
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    out = meeting_bot._account_participants([{'id': 'M1'}], [{'id': 'E1'}])
    names = {p['name']: p['title'] for p in out}
    assert names == {'Jane Doe': 'VP Ops', 'Mark Lee': None}


def test_account_participants_degrades(monkeypatch):
    monkeypatch.setattr(meeting_bot.requests, 'post', lambda *a, **k: _Resp(500, {}))
    assert meeting_bot._account_participants([{'id': 'M1'}], []) == []


def test_contacts_display_caps_at_six(monkeypatch):
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured['n'] = len(json['inputs'])
        return _Resp(200, {'results': []})
    monkeypatch.setattr(meeting_bot.requests, 'post', fake_post)
    meeting_bot._contacts_display([str(i) for i in range(20)])
    assert captured['n'] == 6
