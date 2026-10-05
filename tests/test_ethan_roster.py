import calendar_credit
import meeting_bot
import sheet_sync
import blitz_calendar


def test_ethan_is_a_bdr_everywhere():
    assert '169036459' in calendar_credit.BDR_IDS
    assert meeting_bot.NAME_TO_OWNER['kulp'] == '169036459'
    assert sheet_sync.OWNER_DISPLAY['169036459'] == 'Ethan'
    assert 'ethan@furtherai.com' in blitz_calendar._DEFAULT_BDRS
