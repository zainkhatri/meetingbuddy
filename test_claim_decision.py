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

# --- digest cap + row-rewrite helpers ---

def _row(cid, claimed_by=None):
    if claimed_by:
        return {'type': 'section', 'text': {'type': 'mrkdwn',
                'text': f'*Co {cid}* — Tier 1  ·  {m.CLAIMED_MARK}{claimed_by}'}}
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*Co {cid}* — Tier 1'},
            'accessory': {'type': 'button', 'action_id': 'claim_account', 'value': str(cid),
                          'text': {'type': 'plain_text', 'text': 'Claim'}}}

def _digest(n_open, claimed=()):     # claimed = list of (cid, sdr)
    blocks = [{'type': 'header', 'text': {'type': 'plain_text', 'text': 'Up for grabs'}}]
    blocks += [_row(i) for i in range(n_open)]
    blocks += [_row(cid, by) for cid, by in claimed]
    return blocks

def test_claim_cap_even_split():
    assert m.claim_cap(15) == 3 and m.claim_cap(10) == 2 and m.claim_cap(3) == 1
    assert m.claim_cap(12) == 3            # ceil(12/5)
    assert m.claim_cap(0) == 0

def test_count_company_rows_counts_open_and_claimed():
    blocks = _digest(13, claimed=[(100, 'Zain'), (101, 'Zain')])   # 15 total, header ignored
    assert m.count_company_rows(blocks) == 15

def test_cap_ok_blocks_at_limit():
    # 15-row digest -> cap 3. Zain already has 2 -> ok; give a 3rd -> not ok.
    ok, cap, used = m.cap_ok(_digest(13, [(100, 'Zain'), (101, 'Zain')]), 'Zain')
    assert ok is True and cap == 3 and used == 2
    ok2, cap2, used2 = m.cap_ok(_digest(12, [(100, 'Zain'), (101, 'Zain'), (102, 'Zain')]), 'Zain')
    assert ok2 is False and cap2 == 3 and used2 == 3

def test_cap_is_per_bdr():
    blocks = _digest(12, [(100, 'Zain'), (101, 'Zain'), (102, 'Zain')])   # Zain maxed at 3
    assert m.cap_ok(blocks, 'Zain')[0] is False
    assert m.cap_ok(blocks, 'Ben')[0] is True                 # Ben still has 0

def test_mark_claimed_rewrites_row_and_drops_button():
    blocks = _digest(3)                                       # cids 0,1,2 with buttons
    out = m.mark_claimed(blocks, 1, 'Dani')
    row = [b for b in out if b['type'] == 'section' and 'Co 1' in b['text']['text']][0]
    assert 'accessory' not in row and f'{m.CLAIMED_MARK}Dani' in row['text']['text']
    # untouched rows keep their buttons
    other = [b for b in out if b['type'] == 'section' and 'Co 0' in b['text']['text']][0]
    assert other.get('accessory', {}).get('action_id') == 'claim_account'
    # and the rewrite makes it count as claimed-by-Dani
    assert m.count_claimed_by(out, 'Dani') == 1

if __name__ == '__main__':
    for fn in [test_claimable_builds_patch, test_already_claimed_this_cycle_rejected, test_self_claim_is_noop_reject,
               test_claim_cap_even_split, test_count_company_rows_counts_open_and_claimed,
               test_cap_ok_blocks_at_limit, test_cap_is_per_bdr, test_mark_claimed_rewrites_row_and_drops_button]:
        fn(); print(f'ok: {fn.__name__}')
    print('all passed')
