#!/usr/bin/env python3
"""Pure recycle logic tests. Run: python3 test_recycle.py"""
from datetime import datetime, timezone, timedelta
import recycle as r
import claim_logic as cl

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _props(days_old=None, state=r.WARM, owner='Ben'):
    p = {'recycle_status': state, 'sdr_owner': owner}
    if days_old is not None:
        when = NOW - timedelta(days=days_old)
        p['hs_last_activity_date'] = str(int(when.timestamp() * 1000))   # epoch-ms like the search API
    return p


# --- date parsing / coldness ---

def test_days_since_activity_epoch_ms():
    assert r.days_since_activity(_props(days_old=34), NOW) == 34

def test_days_since_activity_iso():
    p = {'hs_last_activity_date': '2026-08-01T00:00:00Z'}
    assert r.days_since_activity(p, NOW) == 51

def test_missing_activity_is_none_and_not_cold():
    assert r.days_since_activity({}, NOW) is None
    assert r.is_cold({}, NOW, 30) is False           # never act on missing data
    assert r.is_cold({'hs_last_activity_date': ''}, NOW, 30) is False

def test_falls_back_to_createdate_when_no_activity():
    # never-worked account (blank activity date) ages by createdate instead
    p = {'hs_last_activity_date': '', 'createdate': '2026-08-01T00:00:00Z'}
    assert r.days_since_activity(p, NOW) == 51
    assert r.is_cold(p, NOW, 30) is True

def test_activity_date_wins_over_createdate():
    # a worked account uses last-activity, not the older createdate
    p = {'hs_last_activity_date': '2026-09-20T00:00:00Z', 'createdate': '2026-01-01T00:00:00Z'}
    assert r.days_since_activity(p, NOW) == 1
    assert r.is_cold(p, NOW, 30) is False

def test_fresh_import_not_cold_yet():
    # bulk-imported a day ago, no activity -> ages by createdate -> not cold yet
    p = {'createdate': '2026-09-20T00:00:00Z'}      # NOW is 2026-09-21 12:00
    assert r.days_since_activity(p, NOW) == 1
    assert r.is_cold(p, NOW, 27) is False

def test_is_cold_boundary():
    assert r.is_cold(_props(days_old=30), NOW, 30) is True
    assert r.is_cold(_props(days_old=29), NOW, 30) is False


# --- state machine: warn phase ---

def test_warn_fires_at_threshold():
    d = r.decide(_props(days_old=28, state=r.WARM), NOW, 'warn')
    assert d['action'] == 'warn' and d['new_state'] == r.WARNED

def test_warn_noop_when_too_fresh():
    d = r.decide(_props(days_old=10, state=r.WARM), NOW, 'warn')
    assert d['action'] == 'noop'

def test_warn_noop_when_already_warned():
    d = r.decide(_props(days_old=28, state=r.WARNED), NOW, 'warn')
    assert d['action'] == 'noop'                     # don't re-warn

def test_claimed_active_account_re_enters_clock_when_cold():
    # an 'active' (recently claimed) account that later goes cold warns again
    d = r.decide(_props(days_old=45, state=r.ACTIVE), NOW, 'warn')
    assert d['action'] == 'warn' and d['new_state'] == r.WARNED


# --- state machine: release phase ---

def test_release_fires_when_still_cold_after_warning():
    d = r.decide(_props(days_old=31, state=r.WARNED), NOW, 'release')
    assert d['action'] == 'release' and d['new_state'] == r.POOL

def test_release_needs_prior_warning():
    # never warned -> Monday doesn't release straight from warm
    d = r.decide(_props(days_old=31, state=r.WARM), NOW, 'release')
    assert d['action'] == 'noop'

def test_pool_row_untouched_on_release():
    d = r.decide(_props(days_old=40, state=r.POOL), NOW, 'release')
    assert d['action'] == 'noop'                     # already posted; leave it


# --- reset: owner re-engaged / deal now covers it ---

def test_fresh_activity_resets_warned():
    d = r.decide(_props(days_old=2, state=r.WARNED), NOW, 'release')
    assert d['action'] == 'reset' and d['new_state'] == r.WARM

def test_fresh_activity_resets_pool():
    d = r.decide(_props(days_old=1, state=r.POOL), NOW, 'warn')
    assert d['action'] == 'reset' and d['new_state'] == r.WARM

def test_covered_deal_never_warns():
    d = r.decide(_props(days_old=90, state=r.WARM), NOW, 'warn', has_covered_deal=True)
    assert d['action'] == 'noop'

def test_covered_deal_pulls_out_of_pool():
    d = r.decide(_props(days_old=90, state=r.POOL), NOW, 'release', has_covered_deal=True)
    assert d['action'] == 'reset' and d['new_state'] == r.WARM


# --- DM copy ---

def test_warn_dm_names_account_deadline_action():
    txt = r.warn_dm_text('Acme Insurance', 'Mon Sep 28', 3)
    assert 'Acme Insurance' in txt and 'Sep 28' in txt
    assert 'keep it' in txt.lower()                  # tells them the saving action


# --- digest blocks round-trip through claim_logic ---

def test_empty_pool_is_empty_blocks():
    assert r.pool_digest_blocks([]) == []

def test_digest_blocks_shape_and_claim_logic_compat():
    accts = [{'id': 100, 'name': 'Acme', 'note': 'cold 34d'},
             {'id': 101, 'name': 'Globex'}]
    blocks = r.pool_digest_blocks(accts)
    assert blocks[0]['type'] == 'header'
    # claim_logic must see exactly the two claimable rows and be able to mark one
    assert cl.count_company_rows(blocks) == 2
    out = cl.mark_claimed(blocks, 100, 'Zain')
    assert cl.count_claimed_by(out, 'Zain') == 1
    row = [b for b in blocks if b.get('accessory', {}).get('value') == '100'][0]
    assert row['accessory']['action_id'] == r.CLAIM_ACTION


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(f'ok: {fn.__name__}')
    print(f'all {len(tests)} passed')
