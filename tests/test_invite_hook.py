"""Glue test: meeting_bot._maybe_offer_invite posts one preview when no invite
exists, and stays silent when AUTO_INVITE is off, an invite exists, or it
already offered on the thread."""
import meeting_bot as mb
import invite_offer


class _Client:
    def __init__(self, replies=None):
        self.posted, self.replies = [], replies or []

    def conversations_replies(self, **_):
        return {'messages': self.replies}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)

    def users_info(self, **_):
        return {'user': {'profile': {'email': 'zain@furtherai.com'}}}


PARSED = {'contact_first_name': 'Tanya', 'contact_last_name': 'Unsworth',
          'contact_email': 'tunsworth@hanover.com', 'company_name': 'The Hanover Insurance Group',
          'meeting_date': '2099-09-30', 'meeting_time_utc': '19:00', 'conference_source': 'itc_2026'}
CO = {'id': '1', 'properties': {'hubspot_owner_id': '165453251'}}


def _run(monkeypatch, *, enabled=True, exists=False, replies=None):
    c = _Client(replies)
    monkeypatch.setattr(mb.app, '_client', c, raising=False)
    monkeypatch.setattr(type(mb.app), 'client', property(lambda self: c))
    monkeypatch.setenv('AUTO_INVITE', '1' if enabled else '0')
    monkeypatch.setattr(mb, '_owner_email', lambda oid: {'88760040': 'zain@furtherai.com',
                                                        '165453251': 'nick@furtherai.com'}.get(oid))
    monkeypatch.setattr(mb, '_owner_name', lambda oid: 'Nick Margay')
    monkeypatch.setattr(mb, '_conf_label', lambda v: 'ITC Vegas 2026')
    monkeypatch.setattr(invite_offer, 'invite_exists', lambda *a, **k: exists)
    mb._maybe_offer_invite(dict(PARSED), CO, None, '88760040', 'U_ZAIN',
                           mb.CONFERENCE_MEETINGS_CHANNEL, '123.456', 15)
    return c


def test_posts_preview_when_no_invite(monkeypatch):
    c = _run(monkeypatch)
    assert len(c.posted) == 1
    p = c.posted[0]
    assert p['thread_ts'] == '123.456'
    assert 'FurtherAI + The Hanover Insurance Group (ITC)' in p['text']
    raw = p['blocks'][1]['elements'][0]['value']
    offer = invite_offer.decode_payload(raw)
    assert offer['organizer'] == 'zain@furtherai.com' and offer['ae_email'] == 'nick@furtherai.com'


def test_silent_when_disabled(monkeypatch):
    assert _run(monkeypatch, enabled=False).posted == []


def test_silent_when_invite_exists(monkeypatch):
    assert _run(monkeypatch, exists=True).posted == []


def test_silent_when_already_offered(monkeypatch):
    c = _run(monkeypatch, replies=[{'text': 'No calendar invite found: FurtherAI + X'}])
    assert c.posted == []


def test_handlers_registered():
    src = open(mb.__file__).read()
    assert "@app.action('invite_send')" in src and "@app.action('invite_skip')" in src
