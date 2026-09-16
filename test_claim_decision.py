#!/usr/bin/env python3
"""Claim decision pure logic. Run: python3 test_claim_decision.py"""
import claim_logic as m

def test_claimable_builds_patch():
    ok, p = m.claim_decision({'sdr_owner': 'Ben', 'recycle_status': ''}, 'Jacob')
    assert ok is True
    assert p['sdr_owner'] == 'Jacob' and p['recycle_status'] == 'active'
    assert p['last_claim_by'] == 'Jacob' and p['claimed_from'] == 'Ben'
    assert p['claim_date']                                   # non-empty iso

def test_already_claimed_this_cycle_rejected():
    ok, p = m.claim_decision({'sdr_owner': 'Dani', 'recycle_status': 'active', 'last_claim_by': 'Dani'}, 'Jacob')
    assert ok is False and 'claimed' in p['reason'].lower()

def test_self_claim_is_noop_reject():
    ok, p = m.claim_decision({'sdr_owner': 'Jacob', 'recycle_status': ''}, 'Jacob')
    assert ok is False                                       # already yours

if __name__ == '__main__':
    for fn in [test_claimable_builds_patch, test_already_claimed_this_cycle_rejected, test_self_claim_is_noop_reject]:
        fn(); print(f'ok: {fn.__name__}')
    print('all passed')
