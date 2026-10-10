"""Encryption at rest for per-user LLM API keys.

The Fernet key comes from LLM_KEY_ENCRYPTION_KEY when set, otherwise it is
derived from JWT_SECRET. In the derived case, rotating JWT_SECRET makes the
stored keys undecryptable -- users simply have to enter their key again.
"""
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from config import settings
import models


def _fernet() -> Fernet:
    if settings.llm_key_encryption_key:
        return Fernet(settings.llm_key_encryption_key.encode())
    digest = hashlib.sha256(b"ragapp:llm-api-key:" + settings.jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def set_llm_api_key(db: Session, user: models.User, api_key: str) -> models.UserSecret:
    secret = db.get(models.UserSecret, user.id) or models.UserSecret(user_id=user.id)
    secret.llm_api_key_encrypted = _fernet().encrypt(api_key.encode()).decode()
    secret.llm_api_key_last4 = api_key[-4:]
    db.add(secret)
    db.commit()
    return secret


def delete_llm_api_key(db: Session, user: models.User) -> None:
    secret = db.get(models.UserSecret, user.id)
    if secret:
        db.delete(secret)
        db.commit()


def get_llm_api_key(db: Session, user_id: int) -> Optional[str]:
    secret = db.get(models.UserSecret, user_id)
    if not secret:
        return None
    try:
        return _fernet().decrypt(secret.llm_api_key_encrypted.encode()).decode()
    except InvalidToken:
        return None  # encryption key changed; the user must re-enter it
