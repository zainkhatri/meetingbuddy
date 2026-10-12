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


def test_slack_text_has_dedup_key_and_ae_ping():
    key = fw.dedup_key('181525873848', '2026-10-08T12:54:53')
    t = fw.slack_text(conference='WSIA Underwriting Summit', contact=CONTACT, form_name='WSIA 2026 Meeting Form',
                      ae_mention='<@U1>', meeting=None, key=key)
    assert key in t and '<@U1>' in t
    import re
    assert re.findall(r'form-sub:\d+:[0-9T:\-]+', t) == [key]


def test_is_test_submission():
    assert fw.is_test_submission({'email': 'janet@furtherai.com', 'firstname': 'janet'})
    assert fw.is_test_submission({'email': 'test2@testing123.com', 'firstname': 'test2'})
    assert fw.is_test_submission({'email': 'x@acme.com', 'firstname': 'testing'})
    assert not fw.is_test_submission({'email': 'dmoler@sompo-intl.com', 'firstname': 'Daniel'})
