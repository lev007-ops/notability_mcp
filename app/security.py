"""Password hashing (stdlib scrypt) and brute-force throttling."""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import threading
import time
from collections import defaultdict, deque

# Format: scrypt:N:r:p:salt:hash (base64url, no '$' so it survives docker-compose .env interpolation)
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, expected = encoded.split(":")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=32
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except Exception:
        return False


def check_hash_format(encoded: str, name: str) -> None:
    parts = encoded.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise RuntimeError(f"{name} is not a valid hash; generate one with: docker compose run --rm app hash-password")


class Throttle:
    """Per-key failure counter with lockout.

    After `max_failures` failures within `window` seconds, the key is locked for `lockout` seconds.
    A global limit protects against distributed guessing.
    """

    def __init__(self, max_failures: int = 5, window: int = 900, lockout: int = 900, global_max: int = 50):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.global_max = global_max
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._global: deque[float] = deque()
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and q[0] < now - self.window:
            q.popleft()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if self._locked_until.get(key, 0) > now:
                return True
            self._trim(self._global, now)
            return len(self._global) >= self.global_max

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            q = self._fails[key]
            q.append(now)
            self._trim(q, now)
            self._global.append(now)
            if len(q) >= self.max_failures:
                self._locked_until[key] = now + self.lockout
                q.clear()

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


class VerifiedCache:
    """Remembers recently verified credentials so WebDAV (Basic auth on every request)
    doesn't pay for scrypt each time. Stores only a keyed HMAC of the credentials."""

    def __init__(self, ttl: int = 600):
        self._key = secrets.token_bytes(32)
        self._ttl = ttl
        self._seen: dict[bytes, float] = {}
        self._lock = threading.Lock()

    def _mac(self, user: str, password: str) -> bytes:
        return hmac.new(self._key, f"{user}\0{password}".encode(), hashlib.sha256).digest()

    def check(self, user: str, password: str) -> bool:
        mac = self._mac(user, password)
        with self._lock:
            exp = self._seen.get(mac)
            return exp is not None and exp > time.time()

    def add(self, user: str, password: str) -> None:
        with self._lock:
            now = time.time()
            self._seen = {k: v for k, v in self._seen.items() if v > now}
            self._seen[self._mac(user, password)] = now + self._ttl


def hash_password_cli() -> None:
    if not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords do not match")
    if len(pw) < 12:
        sys.exit("Use at least 12 characters")
    print(hash_password(pw))
