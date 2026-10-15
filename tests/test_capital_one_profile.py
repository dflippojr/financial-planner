from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.profiles import CAPITAL_ONE_MAPPING
from tests.capital_one_fixtures import NATIVE_CSV


def test_capital_one_preview_parses_debits_and_credits_and_rejects_malformed_row():
    preview = preview_csv(read_csv(NATIVE_CSV), CAPITAL_ONE_MAPPING)
    rows = preview.rows

    assert preview.valid_count == 5
    assert preview.invalid_count == 1
    assert [row.transaction_date.isoformat() for row in rows[:5]] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-18",
    ]
    assert [row.amount_minor for row in rows[:5]] == [-4215, 50000, 1500, -525, -525]
    assert [row.currency for row in rows[:5]] == ["USD"] * 5
    assert not rows[5].is_valid
