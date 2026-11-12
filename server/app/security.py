"""Пароли (Argon2id), токены устройств, защита от перебора."""
import hashlib
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()  # Argon2id, параметры по умолчанию библиотеки
DUMMY_HASH = _ph.hash("dummy-password-used-to-equalize-timing")

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,32}$")
MIN_PASSWORD = 10


def hash_password(pw: str) -> str:
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """Возвращает (токен для устройства, его хеш для БД). В БД сам токен не хранится."""
    t = secrets.token_urlsafe(32)
    return t, token_hash(t)


class Limiter:
    """Блокировка после N неудачных попыток за окно времени."""

    def __init__(self, max_attempts: int, window_s: int):
        self.max = max_attempts
        self.win = window_s
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _clean(self, key: str) -> list[float]:
        now = time.monotonic()
        arr = [t for t in self.fails.get(key, []) if t > now - self.win]
        self.fails[key] = arr
        return arr

    def blocked(self, key: str, limit: int | None = None) -> bool:
        with self.lock:
            return len(self._clean(key)) >= (limit or self.max)

    def fail(self, key: str) -> None:
        with self.lock:
            self._clean(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self.lock:
            self.fails.pop(key, None)
