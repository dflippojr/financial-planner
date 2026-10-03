from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be set.")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


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
