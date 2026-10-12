import form_watch as fw

HEAD = ['Conference', 'Meeting Sourced By', 'Account Executive ', 'Meeting Date', 'Meeting Time',
        'Prospect Company', 'Prospect Name', 'Prospect Email', 'Meeting Status', 'Notes']
HDR = {h.strip(): i + 1 for i, h in enumerate(HEAD)}
CONTACT = {'firstname': 'Daniel', 'lastname': 'Moler', 'email': 'dmoler@sompo-intl.com',
           'company': 'Sompo P&C', 'jobtitle': 'VP, Financial Lines',
           'recent_conversion_date': '2026-10-08T12:54:53.513Z'}


def _mtg(start, outcome='SCHEDULED', title='WSIA - Nia + Daniel'):
    return {'properties': {'hs_meeting_start_time': start, 'hs_meeting_outcome': outcome,
                           'hs_meeting_title': title}}


def test_is_meeting_form():
    assert fw.is_meeting_form('WSIA 2026 Meeting Form')
    assert not fw.is_meeting_form('Meetings Link: janet-wang/demo-request-form')
    assert not fw.is_meeting_form(None)
    assert not fw.is_meeting_form('New book a meeting form (May 5, 2026 4:11:47 PM EDT)')


def test_pick_meeting_skips_cancelled_and_old():
    sub = 1791464093513  # 2026-10-08
    ms = [_mtg('2026-09-01T15:00:00Z'), _mtg('2026-10-13T20:00:00Z', 'CANCELED'),
          _mtg('2026-10-14T18:00:00Z'), _mtg('2026-10-13T22:30:00Z')]
    assert fw.pick_meeting(ms, sub)['properties']['hs_meeting_start_time'] == '2026-10-13T22:30:00Z'
    assert fw.pick_meeting([], sub) is None


def test_build_row_requested_without_meeting():
    row = fw.build_row(conference='WSIA Underwriting Summit', contact=CONTACT, ae_name='Nia',
                       form_name='WSIA 2026 Meeting Form', meeting=None)
    assert row['Meeting Status'] == 'Requested'
    assert row['Meeting Date'] == '' and row['Meeting Sourced By'] == 'Marketing (form)'
    assert row['Prospect Name'] == 'Daniel Moler' and row['Notes'] == 'Inbound via WSIA 2026 Meeting Form (10/8)'


def test_build_row_scheduled_with_meeting():
    row = fw.build_row(conference='WSIA Underwriting Summit', contact=CONTACT, ae_name='Nia',
                       form_name='WSIA 2026 Meeting Form', meeting=_mtg('2026-10-12T22:30:00Z'))
    assert row['Meeting Status'] == 'Scheduled'
    assert row['Meeting Date'] == '10/12' and row['Meeting Time'] == '6:30 PM ET'


def test_find_form_row_by_email_then_name_same_conference_only():
    rows = [HEAD,
            ['ITC 2026', '', '', '', '', 'Sompo', 'Daniel Moler', 'dmoler@sompo-intl.com', '', ''],
            ['WSIA Underwriting Summit', '', '', '', '', 'Sompo', 'Daniel Moler', '', '', '']]
    p = fw.build_row(conference='WSIA Underwriting Summit', contact=CONTACT, ae_name='',
                     form_name='F', meeting=None)
    assert fw.find_form_row(rows, HDR, p) == 3
    assert fw.find_form_row(rows[:2], HDR, p) is None


def test_merge_graduates_requested_row_but_keeps_human_cells():
    cur = ['WSIA Underwriting Summit', 'Marketing (form)', 'Nia', '', '', 'Sompo P&C',
           'Daniel Moler', 'dmoler@sompo-intl.com', 'Requested', 'call his cell']
    p = fw.build_row(conference='WSIA Underwriting Summit', contact=CONTACT, ae_name='Nia',
                     form_name='WSIA 2026 Meeting Form', meeting=_mtg('2026-10-13T16:00:00Z'))
    merged, changed = fw.merge_form_row(HEAD, cur, p, len(HEAD))
    assert changed
    assert merged[3] == '10/13' and merged[8] == 'Scheduled'
    assert merged[9] == 'call his cell'
    again, changed2 = fw.merge_form_row(HEAD, merged, p, len(HEAD))
    assert not changed2 and again == merged


def test_slack_blocks_divide_contacts_and_hide_keys():
    b1 = fw.contact_block(contact=CONTACT, ae_mention='<@U1>', meeting=None)
    b2 = fw.contact_block(contact=dict(CONTACT, firstname='Marissa', lastname='Moscowitz'), ae_mention='<@U2>',
                          meeting=_mtg('2026-10-12T22:30:00Z'))
    blocks = fw.slack_blocks('WSIA Underwriting Summit', [b1, b2])
    assert [b['type'] for b in blocks] == ['section', 'divider', 'section', 'divider', 'section']
    flat = str(blocks)
    assert '<@U1>' in flat and '10/12 6:30 PM ET' in flat
    assert 'form-sub' not in flat and '181525873848' not in flat and '--' not in flat


def test_is_test_submission():
    assert fw.is_test_submission({'email': 'janet@furtherai.com', 'firstname': 'janet'})
    assert fw.is_test_submission({'email': 'test2@testing123.com', 'firstname': 'test2'})
    assert fw.is_test_submission({'email': 'x@acme.com', 'firstname': 'testing'})
    assert not fw.is_test_submission({'email': 'dmoler@sompo-intl.com', 'firstname': 'Daniel'})


class _FakeSlack:
    def __init__(self, history):
        self.history, self.posts, self.edits = history, [], []

    def conversations_history(self, **kw):
        assert 'oldest' not in kw  # Slack drops results with oldest (2026-10-12 spam)
        return {'messages': self.history}

    def chat_postMessage(self, **kw):
        assert 'form-sub' not in kw['text'] + str(kw['blocks'])
        self.posts.append(kw['blocks'])
        self.history.insert(0, {'ts': '9999999999.0', 'text': kw['text'], 'metadata': kw['metadata'],
                                'blocks': kw['blocks']})

    def chat_update(self, **kw):
        self.edits.append(kw['blocks'])
        for m in self.history:
            if m['ts'] == kw['ts']:
                m['blocks'] = kw['blocks']

    def users_list(self, **kw):
        return {'members': []}


ACTIVITY = []


def _patch_io(monkeypatch):
    ACTIVITY.clear()
    sub = {'id': '181525873848', 'properties': dict(CONTACT, recent_conversion_event_name='WSIA 2026 Meeting Form')}
    monkeypatch.setattr(fw, 'search_submissions', lambda hs, now: [sub])
    monkeypatch.setattr(fw, 'contact_meetings', lambda hs, cid: [])
    monkeypatch.setattr(fw, 'owner_info', lambda hs, oid: ('Mike', 'Mike Harris'))
    monkeypatch.setattr(fw, 'upsert_form_row', lambda payload: 'noop')
    monkeypatch.setattr(fw, 'contact_activities', lambda hs, cid: ACTIVITY)
    fw._POSTED.clear()


def test_run_once_posts_once_across_polls(monkeypatch):
    _patch_io(monkeypatch)
    slack = _FakeSlack([{'ts': '1791000000.0', 'text': 'older chatter'}])
    for _ in range(3):
        fw.run_once(hs={'Authorization': 'x'}, slack=slack, channel='C', detect_conference=lambda n: 'wsia_uw_summit')
    assert len(slack.posts) == 1
    fw._POSTED.clear()  # fresh process (redeploy): dedup must come from the hidden metadata alone
    fw.run_once(hs={'Authorization': 'x'}, slack=slack, channel='C', detect_conference=lambda n: 'wsia_uw_summit')
    assert len(slack.posts) == 1


def test_run_once_never_posts_when_history_unreadable(monkeypatch):
    _patch_io(monkeypatch)
    slack = _FakeSlack([])
    c = fw.run_once(hs={'Authorization': 'x'}, slack=slack, channel='C', detect_conference=lambda n: 'wsia_uw_summit')
    assert slack.posts == [] and c['posted'] == 0


def test_pick_outreach_outbound_after_submission_only():
    sub = 1791464093513  # 2026-10-08 12:54 UTC
    acts = [('emails', {'hs_timestamp': '2026-10-12T18:45:44Z', 'hs_email_direction': 'INCOMING_EMAIL'}),
            ('calls', {'hs_timestamp': '2026-09-08T18:45:35Z'}),
            ('emails', {'hs_timestamp': '2026-10-12T18:45:44Z', 'hs_email_direction': 'EMAIL',
                        'hs_email_subject': 'FurtherAI meet up @ WSIA', 'hubspot_owner_id': '162894707'})]
    o = fw.pick_outreach(acts, sub)
    assert o == {'when': '10/12', 'what': 'email: FurtherAI meet up @ WSIA', 'owner_id': '162894707'}
    assert fw.pick_outreach(acts[:2], sub) is None


def test_post_is_edited_in_place_when_ae_reaches_out(monkeypatch):
    _patch_io(monkeypatch)
    slack = _FakeSlack([{'ts': '1791000000.0', 'text': 'older chatter'}])
    run = lambda: fw.run_once(hs={'Authorization': 'x'}, slack=slack, channel='C',
                              detect_conference=lambda n: 'wsia_uw_summit')
    run()
    assert 'No meeting yet' in str(slack.posts[0]) and slack.edits == []
    ACTIVITY.append(('emails', {'hs_timestamp': '2026-10-12T18:45:44Z', 'hs_email_direction': 'EMAIL',
                                'hs_email_subject': 'FurtherAI meet up @ WSIA'}))
    c = run()
    assert c['edited'] == 1 and len(slack.posts) == 1
    assert 'Mike reached out 10/12 (email: FurtherAI meet up @ WSIA)' in str(slack.edits[0])
    assert run()['edited'] == 0  # unchanged state → no further edits


def test_slack_rewritten_text_counts_as_unchanged():
    raw = '*Daniel Moler*, VP @ Sompo P&C\ndmoler@sompo-intl.com'
    stored = '*Daniel Moler*, VP @ Sompo P&amp;C\n<mailto:dmoler@sompo-intl.com|dmoler@sompo-intl.com>'
    assert fw._norm(raw) == fw._norm(stored)
