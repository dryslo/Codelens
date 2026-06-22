"""Хеширование паролей - argon2id (argon2-cffi)."""
from argon2 import PasswordHasher

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Вернуть argon2id-хэш пароля."""
    return _ph.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против сохранённого хэша."""
    try:
        return _ph.verify(stored, password)
    except Exception:
        return False
