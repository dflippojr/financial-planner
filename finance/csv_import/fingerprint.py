import hashlib
from datetime import date


def transaction_fingerprint(account_id: int, transaction_date: date, amount_minor: int, description: str) -> str:
    """SHA-256 of account, date, signed amount, and stored description.

    Inputs are the values that will be stored on import. Corrections later leave
    this digest unchanged, so reimports still match the original identity.
    Newlines separate fields so a description cannot shift the other parts.
    """
    payload = f"{account_id}\n{transaction_date.isoformat()}\n{amount_minor}\n{description}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def manual_entry_fingerprint(
    account_id: int, transaction_date: date, amount_minor: int, description: str, batch_id: int
) -> str:
    """Fingerprint for a hand-entered row, salted with its own one-row batch.

    The batch id keeps a manual entry from ever matching an imported row (or
    another manual entry) during overlap classification, in either direction.
    """
    base = transaction_fingerprint(account_id, transaction_date, amount_minor, description)
    return hashlib.sha256(f"manual\n{batch_id}\n{base}".encode("utf-8")).hexdigest()
