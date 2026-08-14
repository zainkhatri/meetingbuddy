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
