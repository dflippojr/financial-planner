from datetime import date

from finance.csv_import.fingerprint import transaction_fingerprint


def test_fingerprint_is_stable_and_account_scoped():
    shared = dict(transaction_date=date(2026, 9, 27), amount_minor=-1234, description="SYNTHETIC GROCER")
    first = transaction_fingerprint(1, **shared)
    again = transaction_fingerprint(1, **shared)

    assert first == again
    assert len(first) == 64
    assert first != transaction_fingerprint(2, **shared)
    assert first != transaction_fingerprint(1, date(2026, 9, 28), -1234, "SYNTHETIC GROCER")
    assert first != transaction_fingerprint(1, date(2026, 9, 27), -1235, "SYNTHETIC GROCER")
    assert first != transaction_fingerprint(1, date(2026, 9, 27), -1234, "SYNTHETIC CAFE")
