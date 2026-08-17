"""_log_comment: flags only the missing meeting-log fields ('' when complete)."""
import meeting_bot as mb


def _full():
    return {
        'contact_first_name': 'Angela', 'contact_last_name': 'Bowles',
        'contact_title': 'AVP', 'company_name': 'Everest Global',
        'segment': 'carrier', 'company_size': '5000',
        'meeting_date': '2026-10-13', 'source_channel': 'call',
        'location': 'Zoom', 'conference_source': 'wsia_uw_summit',
    }


def test_complete_returns_empty():
    assert mb._log_comment(_full(), is_conference=True) == ''


def test_missing_segment_and_size_only():
    p = _full()
    p['segment'] = None
    p['company_size'] = None
    out = mb._log_comment(p, is_conference=True)
    assert out == '📋 Missing from the meeting log — please add: Segment, Size'
    # only the missing ones — present fields aren't echoed
    assert 'Carrier' not in out and 'Company' not in out


def test_conference_only_required_in_conference_channel():
    p = _full()
    p['conference_source'] = 'other'
    p['conference_name_raw'] = None
    assert 'Conference' in mb._log_comment(p, is_conference=True)
    assert mb._log_comment(p, is_conference=False) == ''


def test_raw_conference_name_satisfies_conference():
    p = _full()
    p['conference_source'] = 'other'
    p['conference_name_raw'] = 'WSIA'
    assert mb._log_comment(p, is_conference=True) == ''
