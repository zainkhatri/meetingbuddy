"""build_payload conference-display fallback.

A conference auto-created via resolve_or_create_conference has a real HubSpot
slug that sheet_sync's CONFERENCE_DISPLAY map doesn't know. build_payload must
humanize the slug so the row still shows a conference in Ellen's tracker, rather
than landing blank.
"""
import sheet_sync as s


def _call(slug):
    return s.build_payload(
        conference_slug=slug, sourced_by_owner_id=None, meeting_start_ms=None,
        company='Unico', contact_first='Eric', contact_last='', contact_title='',
        contact_email='', hs_meeting_outcome='')


def test_auto_created_slug_humanized():
    assert _call('broker_tech_conference_2026')['Conference'] == 'Broker Tech Conference 2026'


def test_other_slug_stays_blank():
    assert _call('other')['Conference'] == ''


def test_known_slug_uses_display_map():
    known = next(iter(s._KNOWN_CONFERENCES))
    assert _call(known)['Conference'] == s.CONFERENCE_DISPLAY[known]
