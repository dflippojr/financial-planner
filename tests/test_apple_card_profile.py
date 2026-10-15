from finance.csv_import.parser import preview_csv, read_csv
from finance.csv_import.profiles import APPLE_CARD_MAPPING
from tests.apple_card_fixtures import NATIVE_CSV


def test_apple_card_preview_maps_merchant_description_and_inverts_signed_amounts():
    preview = preview_csv(read_csv(NATIVE_CSV), APPLE_CARD_MAPPING)
    valid_rows = [row for row in preview.rows if row.is_valid]

    assert preview.valid_count == 7
    assert preview.invalid_count == 1
    assert [row.transaction_date.isoformat() for row in valid_rows] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
        "2026-09-19",
        "2026-09-20",
    ]
    assert [row.amount_minor for row in valid_rows] == [-450, 50000, 1500, -275, -6210, -6210, -3000]
    assert [row.description for row in valid_rows] == [
        "Synthetic Coffee",
        "Synthetic Card Payment",
        "Synthetic Refund",
        "Synthetic Interest",
        "Synthetic Grocery",
        "Synthetic Grocery",
        "Synthetic, Store",
    ]
    assert all(row.currency == "USD" for row in valid_rows)
    assert all(row.source_transaction_id == "" for row in valid_rows)
    malformed = [row for row in preview.rows if not row.is_valid][0]
    assert "different number of columns" in malformed.errors[0]
