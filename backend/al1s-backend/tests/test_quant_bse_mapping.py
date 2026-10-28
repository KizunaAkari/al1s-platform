from datetime import date

from al1s.quant.bse_mapping import code_for_date


def test_official_code_switch_uses_explicit_mapping_not_suffix_guess():
    assert code_for_date("430047", date(2025, 9, 30)) == "430047"
    assert code_for_date("430047", date(2025, 10, 9)) == "920047"
    assert code_for_date("920047", date(2025, 9, 30)) == "430047"
    assert code_for_date("837023", date(2025, 10, 9)) == "920123"
    assert code_for_date("920999", date(2026, 10, 27)) == "920999"
