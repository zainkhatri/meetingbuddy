import json as _json
import calendar_credit as cc
import pytest

G, F = '163071452', '165453250'  # Gavin, Fabio (both AEs)

def test_no_incumbent_one_ae_assigns():
    assert cc.decide_action(None, {G}) == ('assign', G)

def test_no_incumbent_zero_ae_nudges_no_ae():
    assert cc.decide_action(None, set()) == ('nudge', 'no_ae')

def test_no_incumbent_two_ae_nudges_multi():
    assert cc.decide_action(None, {G, F}) == ('nudge', 'multi_ae')

def test_incumbent_matches_invite_is_noop():
    assert cc.decide_action(G, {G}) == ('noop', None)

def test_incumbent_differs_nudges_conflict():
    assert cc.decide_action(G, {F}) == ('nudge', 'incumbent_conflict')
    assert cc.decide_action(G, set()) == ('nudge', 'incumbent_conflict')
    assert cc.decide_action(G, {G, F}) == ('nudge', 'incumbent_conflict')

def test_nudge_text_no_ae():
    t = cc.nudge_text('no_ae')
    assert 'No AE' in t and 'invite' in t

def test_nudge_text_multi_ae():
    t = cc.nudge_text('multi_ae')
    assert 'Multiple AEs' in t and 'Unassigned' in t

def test_nudge_text_incumbent_uses_name():
    t = cc.nudge_text('incumbent_conflict', incumbent_name='Gavin')
    assert 'Gavin' in t and 'already owns' in t

def test_nudge_text_incumbent_without_name_has_fallback():
    t = cc.nudge_text('incumbent_conflict')
    assert 'already owns' in t


AEMAP = {'gavin@furtherai.com': '163071452', 'fabio@furtherai.com': '165453250'}

class _Resp:
    def __init__(self, payload): self._p = payload; self.ok = True
    def json(self): return self._p

def _ev(attendees):
    return _Resp({'attendees': attendees})

def test_build_ae_email_map_filters_to_aes_and_paginates():
    pages = [
        _Resp({'results': [
                  {'id': '163071452', 'email': 'Gavin@FurtherAI.com'},   # AE, mixed case
                  {'id': '92184259',  'email': 'matt@furtherai.com'}],   # BDR -> excluded
               'paging': {'next': {'after': 'p2'}}}),
        _Resp({'results': [{'id': '165453250', 'email': 'fabio@furtherai.com'}]}),  # AE
    ]
    calls = []
    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        return pages[len(calls) - 1]
    m = cc.build_ae_email_map(http_get=fake_get, api_key='x')
    assert m == {'gavin@furtherai.com': '163071452', 'fabio@furtherai.com': '165453250'}
    assert len(calls) == 2 and calls[1].get('after') == 'p2'


def test_resolver_none_when_no_token():
    r = cc.resolve_ae_from_calendar('bdr@furtherai.com', 'p@acme.com', 'url', 'iso',
                                    AEMAP, token_fn=lambda subject=None: None)
    assert r is None

def test_resolver_deterministic_url_one_ae():
    got = {}
    def http_get(u, headers=None, timeout=None):
        got['url'] = u
        return _ev([{'email': 'p@acme.com'}, {'email': 'gavin@furtherai.com'}])
    r = cc.resolve_ae_from_calendar(
        'bdr@furtherai.com', 'p@acme.com', 'https://cal?eid=abc', '2027-01-01T00:00:00Z',
        AEMAP, token_fn=lambda subject=None: 'tok', http_get=http_get,
        decode_fn=lambda url: ('evt123', 'bdr@furtherai.com'), search_fn=None)
    assert r == {'163071452'}
    assert 'evt123' in got['url']              # deterministic path used, no search

def test_resolver_two_aes():
    r = cc.resolve_ae_from_calendar(
        'bdr@furtherai.com', 'p@acme.com', 'https://cal?eid=abc', 'iso', AEMAP,
        token_fn=lambda subject=None: 'tok',
        http_get=lambda u, headers=None, timeout=None: _ev(
            [{'email': 'gavin@furtherai.com'}, {'email': 'fabio@furtherai.com'}]),
        decode_fn=lambda url: ('evt', 'org@furtherai.com'))
    assert r == {'163071452', '165453250'}

def test_resolver_event_found_but_no_ae_returns_empty_set():
    r = cc.resolve_ae_from_calendar(
        'bdr@furtherai.com', 'p@acme.com', 'https://cal?eid=abc', 'iso', AEMAP,
        token_fn=lambda subject=None: 'tok',
        http_get=lambda u, headers=None, timeout=None: _ev([{'email': 'p@acme.com'}]),
        decode_fn=lambda url: ('evt', 'org@furtherai.com'))
    assert r == set()                          # found, no AE -> empty (NOT None)

def test_resolver_none_when_no_event_after_fallback():
    r = cc.resolve_ae_from_calendar(
        'bdr@furtherai.com', 'p@acme.com', '', 'iso', AEMAP,
        token_fn=lambda subject=None: 'tok',
        decode_fn=lambda url: (None, None),
        search_fn=lambda cal, start, terms: (None, None))
    assert r is None                           # invite-lag -> retry

def test_resolver_fallback_search_finds_ae():
    r = cc.resolve_ae_from_calendar(
        'bdr@furtherai.com', 'p@acme.com', '', 'iso', AEMAP,
        token_fn=lambda subject=None: 'tok',
        http_get=lambda u, headers=None, timeout=None: _ev([{'email': 'gavin@furtherai.com'}]),
        decode_fn=lambda url: (None, None),
        search_fn=lambda cal, start, terms: ('evt-from-search', 'org@furtherai.com'))
    assert r == {'163071452'}


def test_log_owner_change_appends_jsonl(tmp_path):
    p = tmp_path / 'audit.jsonl'
    rec = cc.log_owner_change('deal1', '166833455', '163071452', 'assign',
                              'booking', path=str(p), ts_fn=lambda: 100.0)
    assert rec['deal_id'] == 'deal1' and rec['new_owner'] == '163071452'
    line = p.read_text().strip()
    assert _json.loads(line) == rec
    cc.log_owner_change('deal2', '', '165453250', 'assign', 'cron',
                        path=str(p), ts_fn=lambda: 101.0)
    assert len(p.read_text().strip().splitlines()) == 2   # appends, not overwrites

def test_log_owner_change_rejects_bad_source(tmp_path):
    with pytest.raises(AssertionError):
        cc.log_owner_change('d', '', 'x', 'assign', 'nonsense', path=str(tmp_path / 'a'))


def test_retry_enqueue_dedupes_by_meeting_id():
    cc._RETRY[:] = []
    cc.retry_enqueue({'meeting_id': 'm1', 'deal_id': 'd1'}, now_fn=lambda: 0.0)
    cc.retry_enqueue({'meeting_id': 'm1', 'deal_id': 'd1'}, now_fn=lambda: 1.0)
    assert len(cc.retry_pending(now_fn=lambda: 2.0)) == 1

def test_retry_pending_prunes_expired():
    cc._RETRY[:] = []
    cc.retry_enqueue({'meeting_id': 'm2'}, now_fn=lambda: 0.0)
    assert cc.retry_pending(now_fn=lambda: cc.RETRY_TTL_SEC + 1.0) == []

def test_retry_remove():
    cc._RETRY[:] = []
    cc.retry_enqueue({'meeting_id': 'm3'}, now_fn=lambda: 0.0)
    cc.retry_remove('m3')
    assert cc.retry_pending(now_fn=lambda: 1.0) == []


# --- credit_after_booking ---

BASE = dict(meeting_id='m1', deal_id='d1', deal_owner_id='166833455',  # Unassigned
            booker_email='bdr@furtherai.com', prospect_email='p@acme.com',
            external_url='u', start_iso='iso', ae_email_map=AEMAP,
            owner_name_fn=lambda oid: {'163071452': 'Gavin'}.get(oid),
            now_fn=lambda: 0.0)

def test_credit_none_enqueues_retry_no_text():
    cc._RETRY[:] = []
    out = cc.credit_after_booking(incumbent_ae=None, resolve_fn=lambda **k: None, **BASE)
    assert out['action'] == 'retry' and out['text'] is None
    assert len(cc.retry_pending(now_fn=lambda: 1.0)) == 1

def test_credit_one_ae_nudge_only_does_not_assign():
    calls = []
    out = cc.credit_after_booking(
        incumbent_ae=None, resolve_fn=lambda **k: {'163071452'},
        assign_enabled=False, assign_fn=lambda **k: calls.append(k), **BASE)
    assert out['action'] == 'assign' and out['assigned_to'] == '163071452'
    assert out['text'] is None            # nudge-only phase posts nothing on assign
    assert calls == []                    # writer NOT called while disabled

def test_credit_multi_ae_posts_nudge():
    out = cc.credit_after_booking(
        incumbent_ae=None, resolve_fn=lambda **k: {'163071452', '165453250'}, **BASE)
    assert out['action'] == 'nudge' and 'Multiple AEs' in out['text']

def test_credit_incumbent_conflict_names_incumbent():
    out = cc.credit_after_booking(
        incumbent_ae='163071452', resolve_fn=lambda **k: {'165453250'}, **BASE)
    assert out['action'] == 'nudge' and 'Gavin' in out['text']

def test_credit_never_overwrites_human_non_ae_owner():
    # A deal claimed by a human who is neither an AE nor a BDR (e.g. a manager,
    # owner id 99496285) must NOT be reassigned even with auto-assign on.
    calls = []
    base = {**BASE, 'deal_owner_id': '99496285'}
    out = cc.credit_after_booking(
        incumbent_ae=None, resolve_fn=lambda **k: {'163071452'},
        assign_enabled=True, assign_fn=lambda **k: calls.append(k), **base)
    assert out['action'] == 'assign' and out['assigned_to'] == '163071452'
    assert calls == []                    # writer NOT called — human owner protected

def test_credit_writes_when_unassigned_and_enabled():
    calls = []
    out = cc.credit_after_booking(
        incumbent_ae=None, resolve_fn=lambda **k: {'163071452'},
        assign_enabled=True, assign_fn=lambda **k: calls.append(k), **BASE)
    assert calls == [{'deal_id': 'd1', 'owner_id': '163071452'}]

def test_credit_writes_when_bdr_owned_and_enabled():
    calls = []
    base = {**BASE, 'deal_owner_id': '92184259'}  # Matt (BDR)
    cc.credit_after_booking(
        incumbent_ae=None, resolve_fn=lambda **k: {'163071452'},
        assign_enabled=True, assign_fn=lambda **k: calls.append(k), **base)
    assert calls == [{'deal_id': 'd1', 'owner_id': '163071452'}]

def test_bdr_and_ae_rosters_are_disjoint():
    assert cc.BDR_IDS.isdisjoint(cc.AE_IDS)
    assert cc.UNASSIGNED not in cc.AE_IDS and cc.UNASSIGNED not in cc.BDR_IDS


def test_run_retry_once_removes_resolved_items():
    cc._RETRY[:] = []
    cc.retry_enqueue({'meeting_id': 'mA'}, now_fn=lambda: 0.0)
    cc.retry_enqueue({'meeting_id': 'mB'}, now_fn=lambda: 0.0)
    # mA resolves (assign), mB still lagging (retry)
    def credit_fn(ctx):
        return {'action': 'assign'} if ctx['meeting_id'] == 'mA' else {'action': 'retry'}
    still = cc.run_retry_once(credit_fn=credit_fn, now_fn=lambda: 1.0)
    ids = {e['meeting_id'] for e in cc.retry_pending(now_fn=lambda: 1.0)}
    assert ids == {'mB'} and still == 1
