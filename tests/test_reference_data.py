"""Reference data: FATF list history and the CDD rules table."""
import pandas as pd

# Published FATF changes during the period (independent of generate_data.py's own logic)
OCT_2024_INCREASED_MONITORING = {
    "Algeria", "Angola", "Bulgaria", "Burkina Faso", "Cameroon", "Cote d'Ivoire", "Croatia",
    "Democratic Republic of the Congo", "Haiti", "Kenya", "Lebanon", "Mali", "Monaco", "Mozambique",
    "Namibia", "Nigeria", "Philippines", "South Africa", "South Sudan", "Syria", "Tanzania",
    "Venezuela", "Vietnam", "Yemen",
}
CHANGES = {  # version: (removed, added)
    "2025-02": ({"Philippines"}, {"Lao PDR", "Nepal"}),
    "2025-06": ({"Croatia", "Mali", "Tanzania"}, {"Bolivia", "Virgin Islands (UK)"}),
    "2025-10": ({"Burkina Faso", "Mozambique", "Nigeria", "South Africa"}, set()),
}


def listed(hist, version, status):
    h = hist[(hist.list_version == version) & (hist.fatf_status == status)]
    return set(h.country_name)


def test_four_list_versions_with_correct_dates(raw):
    h = raw("country_risk_history")
    v = h.drop_duplicates("list_version").set_index("list_version")
    assert list(v.index) == ["2024-10", "2025-02", "2025-06", "2025-10"]
    assert list(v.effective_from) == ["2024-10-25", "2025-02-21", "2025-06-13", "2025-10-24"]
    # each version ends the day before the next one starts; the latest is open-ended
    assert list(v.effective_to.fillna("open")) == ["2025-02-20", "2025-06-12", "2025-10-23", "open"]


def test_call_for_action_list_unchanged(raw):
    h = raw("country_risk_history")
    for ver in ["2024-10", "2025-02", "2025-06", "2025-10"]:
        assert listed(h, ver, "Call for action") == {
            "Democratic People's Republic of Korea", "Iran", "Myanmar"}


def test_increased_monitoring_follows_published_changes(raw):
    h = raw("country_risk_history")
    expected = set(OCT_2024_INCREASED_MONITORING)
    assert listed(h, "2024-10", "Increased monitoring") == expected
    for ver, (removed, added) in CHANGES.items():
        expected = (expected - removed) | added
        assert listed(h, ver, "Increased monitoring") == expected, ver
    assert len(expected) == 20          # October 2025 list


def test_uk_high_risk_flag_matches_fatf_status(raw):
    h = raw("country_risk_history")
    assert ((h.fatf_status != "Not listed").astype(int) == h.uk_high_risk_third_country).all()


def test_rules_table_complete_and_sourced(raw):
    r = raw("cdd_rules")
    assert list(r.rule_code) == [f"R{i:02d}" for i in range(1, 13)]
    assert r.legal_basis.notna().all() and r.basis_type.notna().all()
    # thresholds that no regulation sets must be labelled illustrative
    for code in ["R06", "R07", "R08", "R09", "R12"]:
        assert "Illustrative" in r.set_index("rule_code").loc[code, "basis_type"], code


def test_answer_key_has_67_planted_issues(raw):
    e = raw("expected_issues")
    assert len(e) == 67
    assert e.check.nunique() == 11
