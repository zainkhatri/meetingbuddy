import meeting_bot as mb


def test_slugify_basic():
    assert mb.slugify_conference("Broker Tech Conference", 2026) == (
        "broker_tech_conference_2026", "Broker Tech Conference 2026")

def test_slugify_acronym_and_punctuation():
    assert mb.slugify_conference("BTC", 2026) == ("btc_2026", "BTC 2026")
    assert mb.slugify_conference("R.I.M.S. RiskWorld!", 2026) == (
        "r_i_m_s_riskworld_2026", "R.I.M.S. RiskWorld! 2026")

def test_slugify_junk_returns_none():
    assert mb.slugify_conference("", 2026) is None
    assert mb.slugify_conference("   ", 2026) is None
    assert mb.slugify_conference("2026", 2026) is None   # all-digit name
    assert mb.slugify_conference("Booth", None) is None  # no year → can't scope
