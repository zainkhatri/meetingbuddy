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


def _run(monkeypatch, *, enabled=True, exists=False, replies=None, synced_url=None):
    c = _Client(replies)
    monkeypatch.setattr(mb.app, '_client', c, raising=False)
    monkeypatch.setattr(type(mb.app), 'client', property(lambda self: c))
    monkeypatch.setenv('AUTO_INVITE', '1' if enabled else '0')
    monkeypatch.setenv('INVITE_OFFER_DELAY_S', '0')
    monkeypatch.setattr(mb, '_invite_roster', lambda: ['fabio@furtherai.com'])
    monkeypatch.setattr(mb, '_owner_email', lambda oid: {'88760040': 'zain@furtherai.com',
                                                        '165453251': 'nick@furtherai.com'}.get(oid))
    monkeypatch.setattr(mb, '_owner_name', lambda oid: 'Nick Margay')
    monkeypatch.setattr(mb, '_conf_label', lambda v: 'ITC Vegas 2026')
    monkeypatch.setattr(invite_offer, 'invite_exists', lambda *a, **k: exists)
    mb._maybe_offer_invite(dict(PARSED), CO, None, '88760040', 'U_ZAIN',
                           mb.CONFERENCE_MEETINGS_CHANNEL, '123.456', 15, synced_url=synced_url)
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


def test_silent_when_hubspot_meeting_is_calendar_synced(monkeypatch):
    assert _run(monkeypatch, synced_url='https://www.google.com/calendar/event?eid=abc').posted == []


def test_team_check_scans_roster(monkeypatch):
    seen = {}

    def fake_exists(cals, *a, **k):
        seen['cals'] = cals
        return False
    monkeypatch.setattr(invite_offer, 'invite_exists', fake_exists)
    offer = {'organizer': 'zain@furtherai.com', 'ae_email': 'nick@furtherai.com',
             'prospect_email': 'p@x.com', 'company': 'X', 'start_utc': '2099-01-01T00:00:00Z',
             'prospect_name': 'P Q'}
    monkeypatch.setattr(mb, '_invite_roster', lambda: ['fabio@furtherai.com', 'nick@furtherai.com'])
    assert mb._invite_check(offer) is False
    assert seen['cals'] == ['zain@furtherai.com', 'nick@furtherai.com', 'fabio@furtherai.com']


def test_restart_waits_for_pending_offer(monkeypatch):
    import threading, time as _t
    mb._INVITE_PENDING.add('1.1')
    threading.Timer(0.2, lambda: mb._INVITE_PENDING.discard('1.1')).start()
    t0 = _t.time()
    assert mb._wait_for_pending_invites(max_s=5, poll_s=0.05) == 0
    assert _t.time() - t0 < 2


def test_restart_wait_is_bounded():
    mb._INVITE_PENDING.add('2.2')
    try:
        assert mb._wait_for_pending_invites(max_s=0.2, poll_s=0.05) == 1
    finally:
        mb._INVITE_PENDING.discard('2.2')


def test_delayed_offer_runs_and_clears_pending(monkeypatch):
    import time as _t
    ran = []
    monkeypatch.setenv('AUTO_INVITE', '1')
    monkeypatch.setenv('INVITE_OFFER_DELAY_S', '0.1')
    monkeypatch.setattr(mb, '_offer_invite_now', lambda *a: ran.append(a[6]))
    mb._maybe_offer_invite({}, None, None, 'o', 'U1', mb.CONFERENCE_MEETINGS_CHANNEL, '9.9', 15)
    assert '9.9' in mb._INVITE_PENDING
    _t.sleep(0.4)
    assert ran == ['9.9'] and '9.9' not in mb._INVITE_PENDING


def test_every_process_booking_caller_passes_poster():
    import re
    src = open(mb.__file__).read()
    calls = [m for m in re.findall(r"_process_booking\(([^\n]*)\)", src) if not m.startswith('parsed, text, owner_id, ts, client, say, channel=None')]
    assert calls and all('poster=' in c for c in calls), calls


def test_allowlist_limits_offers_to_listed_posters(monkeypatch):
    monkeypatch.setenv('INVITE_POSTERS', 'U_SOMEONE_ELSE')
    assert _run(monkeypatch).posted == []
    monkeypatch.setenv('INVITE_POSTERS', 'U_ZAIN,U_OTHER')
    assert len(_run(monkeypatch).posted) == 1


def test_no_allowlist_means_everyone(monkeypatch):
    monkeypatch.delenv('INVITE_POSTERS', raising=False)
    assert len(_run(monkeypatch).posted) == 1


class _ActClient:
    def __init__(self): self.updates, self.eph = [], []
    def chat_update(self, **kw): self.updates.append(kw)
    def chat_postEphemeral(self, **kw): self.eph.append(kw)


def _click(monkeypatch, status, has_buttons):
    offer = {'organizer': 'zain@furtherai.com', 'poster_slack': 'U1', 'prospect_name': 'P Q',
             'prospect_email': 'p@x.com', 'company': 'X', 'start_utc': '2099-01-01T00:00:00Z',
             'duration_min': 15, 'is_conference': True, 'conf_short': 'ITC', 'location': 'B',
             'ae_email': '', 'ae_name': ''}
    monkeypatch.setattr(invite_offer, 'send_invite', lambda *a, **k: {'status': status})
    c = _ActClient()
    blocks = [{'type': 'actions'}] if has_buttons else [{'type': 'section'}]
    body = {'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1.2', 'blocks': blocks}}
    mb.handle_invite_send(lambda: None, body, c, {'value': invite_offer.encode_payload(offer)})
    return c


def test_first_click_updates_message(monkeypatch):
    c = _click(monkeypatch, 'sent', True)
    assert len(c.updates) == 1 and 'Invite sent' in c.updates[0]['blocks'][0]['text']['text']


def test_late_second_click_does_not_overwrite_success(monkeypatch):
    c = _click(monkeypatch, 'already_exists', False)
    assert c.updates == [] and any('already' in e['text'] for e in c.eph)


def test_non_poster_click_rejected(monkeypatch):
    offer_body = {'user': {'id': 'U_RANDO'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1.2', 'blocks': []}}
    c = _ActClient()
    called = []
    monkeypatch.setattr(invite_offer, 'send_invite', lambda *a, **k: called.append(1))
    offer = {k: '' for k in invite_offer._PAYLOAD_KEYS}
    offer.update(poster_slack='U1', start_utc='2099-01-01T00:00:00Z', prospect_email='p@x.com')
    mb.handle_invite_send(lambda: None, offer_body, c, {'value': invite_offer.encode_payload(offer)})
    assert called == [] and 'Only the person' in c.eph[0]['text']
