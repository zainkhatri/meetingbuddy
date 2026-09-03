"""hs_find_existing_meeting: a contact's LONE alive meeting is matched even when it
drifted far from the announced date (the reschedule gap that left meeting_sourced_by
blank — e.g. Excellus announced Jun 17, meeting moved to Sep 17). A strict ±5-day
window still applies once a contact has several meetings (ambiguous — don't guess)."""
import meeting_bot


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _install(monkeypatch, assoc_mids, meeting_props):
    """assoc_mids: meeting ids associated to the contact.
       meeting_props: {mid: properties dict} returned for each meeting GET."""
    def fake_get(url, headers=None, params=None, timeout=None):
        if '/associations/meetings' in url:
            return _Resp(200, {'results': [{'toObjectId': m} for m in assoc_mids]})
        for mid, props in meeting_props.items():
            if f'/objects/meetings/{mid}' in url:
                return _Resp(200, {'properties': props})
        raise AssertionError(f'unexpected GET {url}')
    monkeypatch.setattr(meeting_bot.requests, 'get', fake_get)


def test_lone_rescheduled_meeting_matched_far_from_announced_date(monkeypatch):
    # Announced Jun 17; the single meeting was rescheduled to Sep 17 (~92d away).
    _install(monkeypatch, ['M1'], {
        'M1': {'hs_meeting_start_time': '2026-09-17T15:30:00Z',
               'hs_meeting_title': 'FurtherAI + Excellus (reschedule from June)',
               'meeting_sourced_by': '', 'hubspot_owner_id': '', 'hs_meeting_outcome': 'SCHEDULED'},
    })
    got = meeting_bot.hs_find_existing_meeting('C1', '2026-06-17')
    assert got and got['id'] == 'M1'                      # lone meeting -> matched despite drift


def test_multiple_meetings_keep_strict_window(monkeypatch):
    # Two alive meetings, both far from announced Jun 17 -> ambiguous -> no match (unchanged).
    _install(monkeypatch, ['M1', 'M2'], {
        'M1': {'hs_meeting_start_time': '2026-06-04T13:00:00Z',
               'hs_meeting_title': 'FurtherAI + Excellus (InsurTech)',
               'meeting_sourced_by': '', 'hubspot_owner_id': ''},
        'M2': {'hs_meeting_start_time': '2026-09-17T15:30:00Z',
               'hs_meeting_title': 'FurtherAI + Excellus (reschedule)',
               'meeting_sourced_by': '', 'hubspot_owner_id': ''},
    })
    assert meeting_bot.hs_find_existing_meeting('C1', '2026-06-17') is None


def test_canceled_lone_meeting_not_matched(monkeypatch):
    # The only meeting is canceled -> filtered out before the lone check -> None.
    _install(monkeypatch, ['M1'], {
        'M1': {'hs_meeting_start_time': '2026-09-17T15:30:00Z',
               'hs_meeting_title': 'FurtherAI + Excellus', 'hs_meeting_outcome': 'CANCELED',
               'meeting_sourced_by': '', 'hubspot_owner_id': ''},
    })
    assert meeting_bot.hs_find_existing_meeting('C1', '2026-06-17') is None


def test_lone_meeting_within_window_still_matches(monkeypatch):
    # Sanity: a normal in-window lone match is unaffected by the new branch.
    _install(monkeypatch, ['M1'], {
        'M1': {'hs_meeting_start_time': '2026-06-18T13:00:00Z',
               'hs_meeting_title': 'FurtherAI + Excellus',
               'meeting_sourced_by': '', 'hubspot_owner_id': ''},
    })
    got = meeting_bot.hs_find_existing_meeting('C1', '2026-06-17')
    assert got and got['id'] == 'M1'
