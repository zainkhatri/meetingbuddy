# tests/test_conference_reply.py
import meeting_bot as mb


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


def test_find_meeting_by_booked_at_hit(monkeypatch):
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured['json'] = json
        return _Resp(200, {'total': 1, 'results': [{
            'id': '42',
            'properties': {'conference_source': 'other',
                           'hs_meeting_start_time': '1786662392000'}}]})
    monkeypatch.setattr(mb.requests, 'post', fake_post)
    out = mb._find_meeting_by_booked_at('1786662392.092149')
    assert out == {'id': '42', 'conference_source': 'other', 'meeting_date': '2026-08-13'}
    # searched by booked_at in ms
    f = captured['json']['filterGroups'][0]['filters'][0]
    assert f == {'propertyName': 'booked_at', 'operator': 'EQ', 'value': '1786662392092'}


def test_find_meeting_by_booked_at_miss(monkeypatch):
    monkeypatch.setattr(mb.requests, 'post',
                        lambda *a, **k: _Resp(200, {'total': 0, 'results': []}))
    assert mb._find_meeting_by_booked_at('1786662392.092149') is None


def test_find_meeting_by_booked_at_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('network')
    monkeypatch.setattr(mb.requests, 'post', boom)
    assert mb._find_meeting_by_booked_at('1786662392.092149') is None


def _stub_meeting(monkeypatch, meeting):
    monkeypatch.setattr(mb, '_find_meeting_by_booked_at', lambda ts: meeting)

def _capture_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(mb.requests, 'patch',
                        lambda url, headers=None, json=None, timeout=None:
                            calls.append((url, json)) or _Resp(200, {}))
    return calls

def _capture_say():
    said = []
    def say(text=None, thread_ts=None):
        said.append(text)
    return say, said


def test_reply_resolves_existing_conf(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '42', 'conference_source': 'other', 'meeting_date': '2026-08-14'})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: 'wsia_uw_summit')
    monkeypatch.setattr(mb, 'hs_conference_options',
                        lambda force=False: [{'value': 'wsia_uw_summit', 'label': 'WSIA UW Summit 2026'}])
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls[0][0].endswith('/meetings/42')
    assert calls[0][1] == {'properties': {'conference_source': 'wsia_uw_summit'}}
    assert '✓' in said[0] and 'WSIA UW Summit 2026' in said[0]


def test_reply_creates_new_conf(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '7', 'conference_source': None, 'meeting_date': '2026-09-01'})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: None)
    monkeypatch.setattr(mb, 'resolve_or_create_conference',
                        lambda raw, date: {'value': 'broker_tech_conference_2026',
                                           'created': True, 'label': 'Broker Tech Conference 2026'})
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'Broker Tech Conference', say)
    assert calls[0][1] == {'properties': {'conference_source': 'broker_tech_conference_2026'}}
    assert 'Broker Tech Conference 2026' in said[0]


def test_reply_noop_when_no_meeting(monkeypatch):
    _stub_meeting(monkeypatch, None)
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls == [] and said == []


def test_reply_noop_when_already_tagged(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '9', 'conference_source': 'tmpaa', 'meeting_date': '2026-08-14'})
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'WSIA', say)
    assert calls == [] and said == []


def test_reply_unresolved(monkeypatch):
    _stub_meeting(monkeypatch, {'id': '3', 'conference_source': 'other', 'meeting_date': None})
    monkeypatch.setattr(mb, 'detect_conference_from_title', lambda t: None)
    monkeypatch.setattr(mb, 'resolve_or_create_conference', lambda raw, date: None)
    calls = _capture_patch(monkeypatch)
    say, said = _capture_say()
    mb._handle_conference_reply('1786662392.092149', 'asdf', say)
    assert calls == []
    assert 'Still couldn\'t identify' in said[0]


def test_is_conference_reply_true():
    ev = {'thread_ts': '111.1', 'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is True

def test_is_conference_reply_false_toplevel():
    # top-level booking post: thread_ts absent
    ev = {'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is False

def test_is_conference_reply_false_parent_equals_ts():
    # Slack sets thread_ts == ts on a thread PARENT; that's still a booking, not a reply
    ev = {'thread_ts': '222.2', 'channel': mb.CONFERENCE_MEETINGS_CHANNEL}
    assert mb._is_conference_reply(ev, '222.2') is False

def test_is_conference_reply_false_other_channel():
    ev = {'thread_ts': '111.1', 'channel': 'C_OTHER'}
    assert mb._is_conference_reply(ev, '222.2') is False
