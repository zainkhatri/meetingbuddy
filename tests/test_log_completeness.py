"""_log_comment: completed meeting-log block posted as a thread comment."""
import meeting_bot as mb


def _full():
    return {
        'contact_first_name': 'Angela', 'contact_last_name': 'Bowles',
        'contact_title': 'AVP', 'company_name': 'Everest Global',
        'segment': 'carrier', 'company_size': '5000',
        'meeting_date': '2026-10-13', 'source_channel': 'call',
        'location': 'Zoom', 'conference_source': 'wsia_uw_summit',
    }


def test_complete_conference_no_flag():
    out = mb._log_comment(_full(), is_conference=True)
    assert 'Segment: Carrier' in out
    assert 'Size: 5000' in out
    assert 'Conference: wsia_uw_summit' in out
    assert 'Please add' not in out
    assert '⚠️' not in out


def test_missing_segment_and_size_flagged():
    p = _full()
    p['segment'] = None
    p['company_size'] = None
    out = mb._log_comment(p, is_conference=True)
    assert 'Segment: ⚠️ _add_' in out
    assert 'Please add: Segment, Size' in out


def test_conference_only_required_in_conference_channel():
    p = _full()
    p['conference_source'] = 'other'
    p['conference_name_raw'] = None
    assert 'Conference' in mb._log_comment(p, is_conference=True)
    assert 'Conference' not in mb._log_comment(p, is_conference=False)


def test_raw_conference_name_used_when_no_slug():
    p = _full()
    p['conference_source'] = 'other'
    p['conference_name_raw'] = 'WSIA'
    assert 'Conference: WSIA' in mb._log_comment(p, is_conference=True)
