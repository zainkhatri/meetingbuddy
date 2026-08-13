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


def test_canonicalize_fallback_when_no_client(monkeypatch):
    monkeypatch.setattr(mb, "client", None)   # simulate no LLM available
    out = mb.canonicalize_conference("BTC 2026", "2026-09-01")
    assert out == {"name": "BTC", "year": 2026}

def test_canonicalize_year_from_meeting_date(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    out = mb.canonicalize_conference("Broker Tech Conference", "2027-03-10")
    assert out == {"name": "Broker Tech Conference", "year": 2027}

def test_canonicalize_no_year_returns_none(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    assert mb.canonicalize_conference("Broker Tech Conference", None) is None


def test_norm_option_matches_alias():
    # normalization used for dedup: lowercase alnum, drop underscores
    assert mb._norm_opt("Broker Tech Conference 2026") == mb._norm_opt("broker_tech_conference_2026")
    assert mb._norm_opt("WSIA") != mb._norm_opt("WSIA Dinner")


def test_resolve_dedups_to_existing(monkeypatch):
    monkeypatch.setattr(mb, "client", None)  # force raw fallback
    monkeypatch.setattr(mb, "hs_conference_options",
                        lambda force=False: [{"value": "broker_tech_conference_2026",
                                              "label": "Broker Tech Conference 2026", "hidden": False}])
    created = []
    monkeypatch.setattr(mb, "hs_add_conference_option",
                        lambda v, l: created.append((v, l)) or True)
    out = mb.resolve_or_create_conference("Broker Tech Conference", "2026-09-01")
    assert out == {"value": "broker_tech_conference_2026", "created": False,
                   "label": "Broker Tech Conference 2026"}
    assert created == []   # dedup hit → no option written

def test_resolve_creates_new(monkeypatch):
    monkeypatch.setattr(mb, "client", None)
    monkeypatch.setattr(mb, "hs_conference_options", lambda force=False: [])
    created = []
    monkeypatch.setattr(mb, "hs_add_conference_option",
                        lambda v, l: created.append((v, l)) or True)
    out = mb.resolve_or_create_conference("BTC", "2026-09-01")
    assert out == {"value": "btc_2026", "created": True, "label": "BTC 2026"}
    assert created == [("btc_2026", "BTC 2026")]


def test_add_option_refuses_empty_base(monkeypatch):
    monkeypatch.setattr(mb, "hs_conference_options", lambda force=False: [])
    patch_calls = []
    monkeypatch.setattr(mb.requests, "patch", lambda *a, **kw: patch_calls.append((a, kw)))
    assert mb.hs_add_conference_option("x_2026", "X 2026") is False
    assert patch_calls == []


def test_slugify_long_name_keeps_year():
    result = mb.slugify_conference(
        "International Association of Insurance Supervisors Global Summit", 2026)
    assert result is not None
    value, _label = result
    assert value.endswith("_2026")
    assert len(value) <= 50


def test_unsure_reply_fires_on_other_and_none():
    for conf in ('other', None):
        posts = []
        mb._maybe_unsure_reply(mb.CONFERENCE_MEETINGS_CHANNEL, conf,
                               lambda text, thread_ts=None: posts.append((text, thread_ts)), 'ts1')
        assert posts == [("Not sure what conference that is", 'ts1')]


def test_unsure_reply_silent_when_known_or_wrong_context():
    posts = []
    say = lambda text, thread_ts=None: posts.append(text)
    mb._maybe_unsure_reply(mb.CONFERENCE_MEETINGS_CHANNEL, 'tmpaa', say, 'ts')   # known conference
    mb._maybe_unsure_reply('C_OTHER_CHANNEL', 'other', say, 'ts')                # not the conference channel
    mb._maybe_unsure_reply(mb.CONFERENCE_MEETINGS_CHANNEL, None, say, None)      # no ts
    mb._maybe_unsure_reply(mb.CONFERENCE_MEETINGS_CHANNEL, 'other', None, 'ts')  # no say
    assert posts == []
