import meeting_bot


def _filters(body):
    return {f["propertyName"]: f for f in body["filterGroups"][0]["filters"]}


def test_includes_conference_meetings():
    """WSIA/ITC meetings are typed 'conference' — the sweep must see them, not only 'demo'."""
    f = _filters(meeting_bot.exec_attach_query(1_000_000_000_000))
    assert f["meeting_type"]["operator"] == "IN"
    assert set(f["meeting_type"]["values"]) == {"demo", "conference"}


def test_windows_on_start_time_not_create_date():
    """Conference meetings are booked weeks ahead; a create-date window never saw them."""
    now = 1_000_000_000_000
    f = _filters(meeting_bot.exec_attach_query(now))
    assert "hs_createdate" not in f
    st = f["hs_meeting_start_time"]
    assert st["operator"] == "BETWEEN"
    assert int(st["value"]) == now
    assert int(st["highValue"]) == now + 21 * 86400 * 1000


def test_still_requires_synced_invite():
    f = _filters(meeting_bot.exec_attach_query(0))
    assert f["hs_meeting_external_url"]["operator"] == "HAS_PROPERTY"


def test_one_bad_event_does_not_stall_the_pass(monkeypatch):
    """A 403 on one organizer's calendar used to abort the pass, so meetings after it never ran."""
    import types
    seen = []

    def fake_one(vp, m):
        seen.append(m["id"])
        if m["id"] == "bad":
            raise RuntimeError("403 Forbidden")

    resp = types.SimpleNamespace(ok=True, json=lambda: {"results": [{"id": "bad"}, {"id": "good"}]})
    monkeypatch.setattr(meeting_bot, "_exec_attach_one", fake_one)
    monkeypatch.setattr(meeting_bot.requests, "post", lambda *a, **k: resp)
    meeting_bot.exec_attach_pass(None)
    assert seen == ["bad", "good"]
    assert "bad" in meeting_bot._ATTACH_DONE
