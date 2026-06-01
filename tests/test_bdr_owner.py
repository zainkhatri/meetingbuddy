import sheet_sync


def test_known_bdrs_map_to_display_value():
    assert sheet_sync.bdr_sdr_owner_value("88760040") == "Zain"
    assert sheet_sync.bdr_sdr_owner_value("162210484") == "Jacob"
    assert sheet_sync.bdr_sdr_owner_value("82377567") == "Dani"
    assert sheet_sync.bdr_sdr_owner_value("164943105") == "Ben"
    assert sheet_sync.bdr_sdr_owner_value("92184259") == "Matt"


def test_int_owner_id_is_accepted():
    assert sheet_sync.bdr_sdr_owner_value(88760040) == "Zain"


def test_non_bdr_or_blank_returns_empty():
    assert sheet_sync.bdr_sdr_owner_value("654909503") == ""  # Aman (AE/not BDR)
    assert sheet_sync.bdr_sdr_owner_value("") == ""
    assert sheet_sync.bdr_sdr_owner_value(None) == ""
