"""Regression tests for collision-safe attribution matching.

These pin the behavior that stops a booking from being stamped onto the wrong
person's meeting. The live incident: Matt posted "Dave Rose @ Kentucky Farm
Bureau", the old name-only matcher grabbed a *different* "Dave Rose @ bolt", and
the credit landed on Zain's bolt meeting. If you change attribution.py and one
of these fails, you are about to reintroduce that misattribution — fix the code,
not the test.
"""
import attribution as A


class _Resp:
    def __init__(self, results):
        self.status_code = 200
        self._results = results

    def json(self):
        return {'results': self._results}


# --- pure helpers -----------------------------------------------------------

def test_company_tokens_drops_stopwords():
    assert 'liberty' in A.company_tokens('The Liberty Company')
    assert A.company_tokens('Insurance Group') == set()       # all stopwords
    assert A.company_tokens(None) == set()


def test_title_matches_company():
    # the exact bug case: KFB post must NOT match a bolt meeting title
    assert A.title_matches_company('FurtherAI + Kentucky Farm Bureau',
                                   'Kentucky Farm Bureau Mutual Insurance Company')
    assert not A.title_matches_company('FurtherAI + bolt (InsureTech Insights)',
                                       'Kentucky Farm Bureau Mutual Insurance Company')
    # unverifiable company (all stopwords) -> don't block (lenient guard)
    assert A.title_matches_company('anything at all', 'Insurance Group')


# --- find_contact -----------------------------------------------------------

def _patch(monkeypatch, by_email=None, by_name=None):
    def fake_post(url, headers=None, json=None, timeout=None):
        props = {f['propertyName']: f.get('value') for f in json['filterGroups'][0]['filters']}
        if 'email' in props:
            return _Resp(by_email or [])
        return _Resp(by_name or [])
    monkeypatch.setattr(A.requests, 'post', fake_post)


HS = {'Authorization': 'x'}
DAVE_BOLT = {'id': 'BOLT', 'properties': {'firstname': 'Dave', 'lastname': 'Rose', 'company': 'Bolt'}}
DAVE_KFB = {'id': 'KFB', 'properties': {'firstname': 'Dave', 'lastname': 'Rose',
                                        'company': 'Kentucky Farm Bureau Insurance'}}


def test_email_wins(monkeypatch):
    _patch(monkeypatch, by_email=[{'id': 'E', 'properties': {'email': 'x@kyfb.com'}}], by_name=[DAVE_BOLT])
    assert A.find_contact(HS, 'Dave', 'Rose', email='x@kyfb.com')['id'] == 'E'


def test_single_name_match(monkeypatch):
    _patch(monkeypatch, by_name=[DAVE_KFB])
    assert A.find_contact(HS, 'Dave', 'Rose')['id'] == 'KFB'


def test_collision_resolved_by_company(monkeypatch):
    """The incident: same name, company in the post picks the right person."""
    _patch(monkeypatch, by_name=[DAVE_BOLT, DAVE_KFB])
    assert A.find_contact(HS, 'Dave', 'Rose',
                          company_name='Kentucky Farm Bureau Mutual Insurance Company')['id'] == 'KFB'
    assert A.find_contact(HS, 'Dave', 'Rose', company_name='Bolt Insurance')['id'] == 'BOLT'


def test_ambiguous_returns_none(monkeypatch):
    """Same name, posted company matches neither -> refuse to guess."""
    _patch(monkeypatch, by_name=[DAVE_BOLT, DAVE_KFB])
    assert A.find_contact(HS, 'Dave', 'Rose', company_name='Acme Widgets') is None


def test_collision_without_company_returns_none(monkeypatch):
    _patch(monkeypatch, by_name=[DAVE_BOLT, DAVE_KFB])
    assert A.find_contact(HS, 'Dave', 'Rose') is None


def test_no_match_returns_none(monkeypatch):
    _patch(monkeypatch, by_name=[])
    assert A.find_contact(HS, 'Nobody', 'Here', company_name='X') is None
