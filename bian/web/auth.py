"""后台登录：scrypt 加盐哈希、会话（只存 token 的哈希）、CSRF token、登录失败限速。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

SESSION_TTL = 12 * 3600
N, R, P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_problem(pw: str) -> str:
    if len(pw) < 8:
        return "密码至少 8 位"
    if pw.isdigit() or pw.isalpha():
        return "密码请同时包含字母和数字"
    return ""


def random_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if not password_problem(pw):
            return pw


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def has_user(store) -> bool:
    return store.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def set_password(store, username: str, password: str, must_change: bool = False) -> None:
    store.db.execute(
        "INSERT INTO users (username, pw_hash, created, must_change) VALUES (?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, must_change=excluded.must_change",
        (username, hash_password(password), time.time(), int(must_change)))
    store.db.execute("DELETE FROM sessions WHERE username=?", (username,))  # 改密码后旧会话全部失效
    store.db.commit()


def check_login(store, username: str, password: str) -> bool:
    row = store.db.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        verify_password(password, hash_password("dummy-timing"))  # 时间上不暴露用户是否存在
        return False
    return verify_password(password, row[0])


def must_change(store, username: str) -> bool:
    row = store.db.execute("SELECT must_change FROM users WHERE username=?", (username,)).fetchone()
    return bool(row and row[0])


def create_session(store, username: str) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = time.time()
    store.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    store.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (_th(token), username, csrf, now, now + SESSION_TTL))
    store.db.commit()
    return token, csrf


def get_session(store, token: str | None) -> dict | None:
    if not token:
        return None
    row = store.db.execute("SELECT username, csrf, expires FROM sessions WHERE token_hash=?", (_th(token),)).fetchone()
    if not row or row[2] < time.time():
        return None
    return {"username": row[0], "csrf": row[1]}


def delete_session(store, token: str | None) -> None:
    if token:
        store.db.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token),))
        store.db.commit()


class RateLimiter:
    """同一 IP 10 分钟内最多失败 5 次。"""

    def __init__(self, limit: int = 5, window: float = 600):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, ip: str) -> float:
        now = time.time()
        with self.lock:
            hits = [t for t in self.fails.get(ip, []) if now - t < self.window]
            self.fails[ip] = hits
            if len(hits) >= self.limit:
                return self.window - (now - hits[0])
        return 0.0

    def fail(self, ip: str) -> None:
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)
