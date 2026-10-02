import invite_sweep as sw

MIKE, NIA, NORM = '162894707', '84250910', '166089614'
MATT = '92184259'          # BDR
UT = '166833455'
AE_MAP = {'mike@furtherai.com': MIKE, 'nia@furtherai.com': NIA, 'nick.n@furtherai.com': NORM}
EMAILS = {MATT: 'matthew@furtherai.com'}


def _decode(url):
    return ('evt1', url.split('|', 1)[1]) if url and '|' in url else (None, None)


def _aes(m, resolve):
    return sw.aes_for_meeting(m, ae_email_map=AE_MAP, owner_email_fn=EMAILS.get,
                              resolve_fn=resolve, decode_fn=_decode)


def test_hubspot_attendee_ae_counts():
    m = {'attendee_owner_ids': [MIKE, MATT]}
    assert _aes(m, lambda **k: None) == {MIKE}


def test_reads_invite_as_its_organizer():
    seen = []
    def resolve(**k):
        seen.append(k['booker_email'])
        return {NIA}
    m = {'external_url': 'x|daniella@furtherai.com', 'start_iso': '2026-09-01T17:00:00Z'}
    assert _aes(m, resolve) == {NIA}
    assert seen == ['daniella@furtherai.com']


def test_falls_back_to_bdr_calendar_by_prospect_email():
    calls = []
    def resolve(**k):
        calls.append((k['booker_email'], k['external_url'], k['prospect_email']))
        return {MIKE} if k['prospect_email'] == 'a@inszone.com' else None
    m = {'external_url': 'x|someone@inszone.com', 'start_iso': '2026-08-27T20:00:00Z',
         'booker_owner_id': MATT, 'prospect_emails': ['a@inszone.com']}
    assert _aes(m, resolve) == {MIKE}
    assert calls == [('matthew@furtherai.com', None, 'a@inszone.com')]


def test_no_signal_is_empty_set():
    assert _aes({'start_iso': '2026-09-01T17:00:00Z'}, lambda **k: None) == set()


def test_decide_earliest_meeting_with_an_ae_wins():
    assert sw.decide([set(), {MIKE}, {NIA}]) == ('assign', MIKE)


def test_decide_two_aes_on_the_demo_is_ambiguous():
    assert sw.decide([{MIKE, NIA}]) == ('skip', 'multi_ae')


def test_decide_no_ae_anywhere():
    assert sw.decide([set(), set()]) == ('skip', 'no_ae')


def _run(owner_now, meetings_aes, assign_enabled=True):
    writes, logs = [], []
    deal = {'id': 'd1', 'name': 'Inzone - Intro Calls'}
    out = sw.sweep_once(
        fetch_deals=lambda: [deal],
        fetch_meetings=lambda d: [{'i': i} for i in range(len(meetings_aes))],
        aes_fn=lambda m: meetings_aes[m['i']],
        read_owner=lambda d: owner_now,
        assign_fn=lambda **k: writes.append(k),
        log_fn=lambda *a, **k: logs.append(a),
        assign_enabled=assign_enabled)
    return out, writes, logs


def test_sweep_assigns_unassigned_deal_to_invite_ae():
    out, writes, logs = _run(UT, [{MIKE}])
    assert writes == [{'deal_id': 'd1', 'owner_id': MIKE}]
    assert out[0]['action'] == 'assign' and out[0]['wrote'] is True
    assert logs and logs[0][:3] == ('d1', UT, MIKE)


def test_sweep_moves_bdr_owned_deal_to_invite_ae():
    _, writes, _ = _run(MATT, [{NIA}])
    assert writes == [{'deal_id': 'd1', 'owner_id': NIA}]


def test_sweep_never_overwrites_a_human_who_claimed_it():
    out, writes, _ = _run(NIA, [{MIKE}])
    assert writes == [] and out[0]['wrote'] is False


def test_sweep_observation_mode_writes_nothing():
    out, writes, _ = _run(UT, [{MIKE}], assign_enabled=False)
    assert writes == [] and out[0]['action'] == 'assign' and out[0]['wrote'] is False


def test_sweep_leaves_ambiguous_deal_alone():
    out, writes, _ = _run(UT, [{MIKE, NIA}])
    assert writes == [] and out[0] == {'deal_id': 'd1', 'name': 'Inzone - Intro Calls',
                                       'action': 'skip', 'reason': 'multi_ae',
                                       'assigned_to': None, 'wrote': False}


def test_sweep_survives_a_failing_deal():
    def boom(d):
        raise RuntimeError('hubspot 500')
    out = sw.sweep_once(fetch_deals=lambda: [{'id': 'd1'}, {'id': 'd2'}],
                        fetch_meetings=boom, aes_fn=lambda m: set(),
                        read_owner=lambda d: UT, assign_fn=lambda **k: None,
                        log_fn=lambda *a, **k: None, assign_enabled=True)
    assert [o['action'] for o in out] == ['error', 'error']


def test_parse_attendee_owner_ids():
    assert sw.parse_owner_ids('162894707;92184259') == ['162894707', '92184259']
    assert sw.parse_owner_ids(None) == [] and sw.parse_owner_ids('') == []


def test_name_terms_full_name_and_distinctive_word():
    assert sw.name_terms('Burns and Wilcox - Intro Calls') == ['burns and wilcox', 'burns']
    assert sw.name_terms('Portag Mutual - Intro Calls') == ['portag mutual', 'portag']
    assert sw.name_terms('The Mahoney Group - Intro Calls') == ['the mahoney group', 'mahoney']


def test_name_terms_skip_generic_or_short_words():
    assert sw.name_terms('Great American Insurance Group - Intro Calls') == ['great american insurance group']
    assert sw.name_terms('Axon Underwriting - Intro Calls') == ['axon underwriting', 'axon']
    assert sw.name_terms('') == [] and sw.name_terms(None) == []


def test_searches_bdr_calendar_by_company_name_when_no_contact():
    calls = []
    def resolve(**k):
        calls.append(k['prospect_email'])
        return {MIKE} if k['prospect_email'] == 'burns' else None
    m = {'start_iso': '2026-09-08T18:30:00Z', 'booker_owner_id': MATT,
         'prospect_emails': [], 'name_terms': ['burns and wilcox', 'burns']}
    assert _aes(m, resolve) == {MIKE}
    assert calls == ['burns and wilcox', 'burns']
