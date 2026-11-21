import base64
import hashlib
import logging
from typing import Optional, Any
from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

logger = logging.getLogger(__name__)

ENC_PREFIX = "enc::"


def _get_fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if key:
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        return Fernet(raw_key)
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    derived_key = base64.urlsafe_b64encode(digest)
    return Fernet(derived_key)


def encrypt_field(value: Any) -> Optional[str]:
    """Szyfruje wrażliwą wartość na poziomie aplikacji (Fernet AES-128-CBC + HMAC)."""
    if value is None or value == "":
        return None
    text = str(value)
    if text.startswith(ENC_PREFIX):
        return text
    token = _get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")
    return f"{ENC_PREFIX}{token}"


def decrypt_field(value: Any) -> Optional[str]:
    """Deszyfruje wartość zaszyfrowaną przez encrypt_field; obsługuje wstecznie wartości jawne."""
    if value is None or value == "":
        return None
    text = str(value)
    if not text.startswith(ENC_PREFIX):
        return text
    raw_token = text[len(ENC_PREFIX):].encode("utf-8")
    try:
        return _get_fernet().decrypt(raw_token).decode("utf-8")
    except InvalidToken:
        logger.error("Nie udało się odszyfrować pola wrażliwego (błędny klucz lub uszkodzony token).")
        return None

