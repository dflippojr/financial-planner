from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be set.")
    if isinstance(key, str):
        key = key.encode("utf-8")
    try:
        return Fernet(key)
    except (TypeError, ValueError):
        # Fernet's own message is generic, but never chain it: the key must not reach a traceback.
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is not a valid Fernet key.") from None


def check_encryption_key() -> None:
    """Raise ImproperlyConfigured unless secrets can be encrypted with the current key."""
    _fernet()


def encrypt_secret(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_secret(token: bytes) -> str:
    try:
        return _fernet().decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY cannot decrypt this connection.") from exc


def encrypt_access_url(access_url: str) -> bytes:
    return encrypt_secret(access_url)


def decrypt_access_url(token: bytes) -> str:
    return decrypt_secret(token)
