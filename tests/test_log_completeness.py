"""_log_comment: friendly note sharing researched fields + flagging missing ones."""
import meeting_bot as mb


def _full():
    return {
        'contact_first_name': 'Angela', 'contact_last_name': 'Bowles',
        'contact_title': 'AVP', 'company_name': 'Everest Global',
        'segment': 'carrier', 'company_size': '5000',
        'meeting_date': '2026-10-13', 'source_channel': 'call',
        'location': 'Zoom', 'conference_source': 'wsia_uw_summit',
    }


def test_complete_shares_research_and_greets():
    out = mb._log_comment(_full(), is_conference=True, poster='U123')
    assert 'Thanks <@U123>!' in out
    assert '• Segment: Carrier' in out
    assert '• Size: ~5000 employees' in out
    assert 'Could you add' not in out          # nothing missing
    assert 'Nice meeting, Angela!' in out


def test_missing_fields_flagged():
    p = _full()
    p['company_size'] = None
    p['location'] = None
    out = mb._log_comment(p, is_conference=True, poster='U1')
    assert '• Segment: Carrier' in out
    assert 'Could you add to the log: Size, Location?' in out


def test_no_research_uses_logged_opener_and_flags_missing():
    # seg/size null → nothing to share, but they're flagged as missing
    p = {'contact_first_name': 'Bob', 'contact_last_name': 'Lee', 'contact_title': 'VP',
         'company_name': 'Acme', 'segment': None, 'company_size': None,
         'meeting_date': '2026-10-13', 'source_channel': 'call', 'location': 'Zoom'}
    out = mb._log_comment(p, is_conference=False, poster='U1')
    assert 'Logged your meeting with *Acme*.' in out
    assert '• Segment' not in out
    assert 'Could you add to the log: Segment, Size?' in out


def test_no_poster_uses_team():
    out = mb._log_comment(_full(), is_conference=True)
    assert 'Thanks team!' in out
