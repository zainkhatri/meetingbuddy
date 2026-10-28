"""Regression tests for duplicate-contact leaks.

Three ways MeetingBot used to create a duplicate HubSpot contact:
  1. a contact search hit 429/5xx and came back as "no match" -> create;
  2. two same-name contacts, neither matched the posted company -> create;
  3. the company lookup was exact and case-sensitive, so 'Acme Insurance, Inc.'
     missed 'acme insurance' and the new contact was born unassociated.
A failed or ambiguous lookup must never create. Fix the code, not the test.
"""
import pytest

import attribution as A
import meeting_bot as mb

HS = {'Authorization': 'x'}


class _Resp:
    def __init__(self, results=None, status=200):
        self.status_code = status
        self._results = results or []

    def json(self):
        return {'results': self._results}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)


# --- hs_search: retry, then raise -------------------------------------------

def test_search_retries_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(status=429) if len(calls) < 3 else _Resp([{'id': '1'}])
    monkeypatch.setattr(A.requests, 'post', fake_post)
    assert A.hs_search(HS, A.CONTACT_SEARCH_URL, {}) == [{'id': '1'}]
    assert len(calls) == 3


def test_search_raises_after_retries(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(status=503)
    monkeypatch.setattr(A.requests, 'post', fake_post)
    with pytest.raises(RuntimeError):
        A.find_contact(HS, 'Dave', 'Rose')
    assert len(calls) == A.SEARCH_RETRIES


def test_search_raises_on_bad_json(monkeypatch):
    class _Bad(_Resp):
        def json(self):
            raise ValueError('not json')
    monkeypatch.setattr(A.requests, 'post', lambda *a, **k: _Bad())
    with pytest.raises(RuntimeError):
        A.find_contact(HS, 'Dave', 'Rose')


# --- lookup_contact: ambiguity signal ---------------------------------------

DAVE_BOLT = {'id': 'BOLT', 'properties': {'company': 'Bolt'}}
DAVE_KFB = {'id': 'KFB', 'properties': {'company': 'Kentucky Farm Bureau'}}


def test_ambiguous_same_name_flagged(monkeypatch):
    monkeypatch.setattr(A.requests, 'post', lambda *a, **k: _Resp([DAVE_BOLT, DAVE_KFB]))
    assert A.lookup_contact(HS, 'Dave', 'Rose', company_name='Acme') == (None, True)


def test_no_hits_not_ambiguous(monkeypatch):
    monkeypatch.setattr(A.requests, 'post', lambda *a, **k: _Resp([]))
    assert A.lookup_contact(HS, 'Nobody', 'Here') == (None, False)


# --- company matching -------------------------------------------------------

def test_company_key_suffix_case_punct():
    assert A.company_key('Acme Insurance, Inc.') == 'acme insurance'
    assert A.company_key('ACME INSURANCE LLC') == 'acme insurance'
    assert A.company_key('Smith & Jones Co') == 'smith and jones'
    assert A.company_key('Co') == 'co'                    # never strips to empty


def test_email_domain_skips_freemail():
    assert A.email_domain('a@Gmail.com') is None
    assert A.email_domain('a@claims.acme.com') == 'claims.acme.com'
    assert A.email_domain('nope') is None


def test_company_name_match_ignores_suffix_and_case(monkeypatch):
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(json)
        return _Resp([{'id': 'X', 'properties': {'name': 'Acme Mutual Holdings'}},
                      {'id': 'A', 'properties': {'name': 'acme mutual'}}])
    monkeypatch.setattr(A.requests, 'post', fake_post)
    assert A.find_company(HS, 'Acme Mutual, Inc.')['id'] == 'A'
    assert seen[0]['limit'] >= 20 and seen[0]['query'] == 'acme mutual'


def test_company_domain_first(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if 'filterGroups' in json:
            return _Resp([{'id': 'DOM', 'properties': {'name': 'Totally Different',
                                                       'domain': 'www.acme.com'}}])
        raise AssertionError('name search should not run when domain matches')
    monkeypatch.setattr(A.requests, 'post', fake_post)
    assert A.find_company(HS, 'Acme Mutual', email='jo@claims.acme.com')['id'] == 'DOM'


def test_company_freemail_falls_back_to_name(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        assert 'filterGroups' not in json, 'free-mail must not be a domain key'
        return _Resp([{'id': 'A', 'properties': {'name': 'Acme'}}])
    monkeypatch.setattr(A.requests, 'post', fake_post)
    assert A.find_company(HS, 'ACME LLC', email='jo@gmail.com')['id'] == 'A'


def test_company_search_failure_raises(monkeypatch):
    monkeypatch.setattr(A.requests, 'post', lambda *a, **k: _Resp(status=500))
    with pytest.raises(RuntimeError):
        A.find_company(HS, 'Acme')


# --- _process_booking: never create on failed/ambiguous lookup ---------------

class _Stop(Exception):
    pass


PARSED = {'contact_first_name': 'Dave', 'contact_last_name': 'Rose',
          'company_name': 'Acme Insurance, Inc.', 'meeting_date': '2099-09-30'}


def _run(monkeypatch, lookup, company=None):
    created = []
    monkeypatch.setattr(mb, 'hs_find_company', lambda n, e=None: company)
    monkeypatch.setattr(mb, 'hs_company_history', lambda cid: None)
    monkeypatch.setattr(mb, 'hs_lookup_contact', lookup)
    monkeypatch.setattr(mb, 'hs_create_contact',
                        lambda *a, **k: created.append((a, k)) or {'id': 'NEW'})
    monkeypatch.setattr(mb, 'hs_set_contact_sdr_owner', lambda *a: None)
    monkeypatch.setattr(mb, 'hs_associate_contact_company', lambda *a: True)
    monkeypatch.setattr(mb, 'hs_find_existing_meeting', lambda *a: None)

    def stop(*a):
        raise _Stop()
    monkeypatch.setattr(mb, 'hs_find_meeting_by_company_date', stop)
    with pytest.raises(_Stop):   # reached the meeting step: processing continued
        mb._process_booking(dict(PARSED), 'Dave Rose @ Acme', '1', '123.456', None, None,
                            channel='C_TEST')
    return created


def test_failed_search_does_not_create(monkeypatch):
    def boom(*a):
        raise RuntimeError('HubSpot search 503')
    assert _run(monkeypatch, boom) == []


def test_ambiguous_does_not_create(monkeypatch):
    assert _run(monkeypatch, lambda *a: (None, True)) == []


def test_no_match_creates_associated_to_company(monkeypatch):
    created = _run(monkeypatch, lambda *a: (None, False), company={'id': 'CO1', 'properties': {}})
    assert len(created) == 1 and created[0][1]['company_id'] == 'CO1'


def test_create_body_carries_company_association(monkeypatch):
    sent = {}

    class _R:
        status_code = 201

        def json(self):
            return {'id': 'NEW'}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent['body'] = json
        return _R()
    monkeypatch.setattr(mb.requests, 'post', fake_post)
    mb.hs_create_contact('Dave', 'Rose', None, 'Acme', company_id='CO1')
    assoc = sent['body']['associations'][0]
    assert assoc['to']['id'] == 'CO1' and assoc['types'][0]['associationTypeId'] == 279
