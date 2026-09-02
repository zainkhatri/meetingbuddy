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


class _Block:
    def __init__(self, text):
        self.type = 'text'
        self.text = text

class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


def test_summarize_account_calls_claude(monkeypatch):
    captured = {}
    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return _Msg('Two calls since March on claims automation.')
    monkeypatch.setattr(meeting_bot, 'client', type('C', (), {'messages': FakeMessages()})())
    out = meeting_bot._summarize_account(
        [{'hs_meeting_title': 'Intro', 'hs_meeting_body': 'discussed claims'}],
        [{'hs_note_body': 'left vm'}],
        [{'hs_email_subject': 'pricing', 'hs_email_text': 'sent quote'}])
    assert out == 'Two calls since March on claims automation.'
    assert captured['model'] == 'claude-haiku-4-5-20251001'
    assert captured['max_tokens'] == 180
    assert len(captured['messages'][0]['content']) <= 6000


def test_summarize_account_empty_returns_none(monkeypatch):
    monkeypatch.setattr(meeting_bot, 'client', object())  # never called
    assert meeting_bot._summarize_account([], [], []) is None


def test_summarize_account_claude_error_returns_none(monkeypatch):
    class FakeMessages:
        def create(self, **kw):
            raise RuntimeError('boom')
    monkeypatch.setattr(meeting_bot, 'client', type('C', (), {'messages': FakeMessages()})())
    assert meeting_bot._summarize_account([{'hs_meeting_title': 'Intro'}], [], []) is None


def test_last_touch_picks_most_recent(monkeypatch):
    content = {
        'meetings': [{'hs_meeting_start_time': '2026-06-14T10:00:00Z'}],
        'notes': [{'hs_timestamp': '2026-05-01T00:00:00Z'}],
        'emails': [{'hs_timestamp': '2026-08-20T00:00:00Z'}],
    }
    assert meeting_bot._last_touch(content, None) == {'type': 'email', 'date': '2026-08-20'}


def test_last_touch_falls_back_to_activity(monkeypatch):
    content = {'meetings': [], 'notes': [], 'emails': []}
    assert meeting_bot._last_touch(content, '2026-07-01T00:00:00Z') == {'type': 'activity', 'date': '2026-07-01'}
