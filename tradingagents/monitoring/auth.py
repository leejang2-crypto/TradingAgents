"""Single-owner dashboard login. Credentials stay local; sessions stay in memory."""

import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from http.cookies import CookieError, SimpleCookie


COOKIE = "ta_session"
SESSION_SECONDS = 12 * 3600
IDLE_SECONDS = 3600


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**17,
                          r=8, p=1, maxmem=256 * 1024 * 1024).hex()


def register(path: Path, username: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username):
        raise ValueError("ID: use 3-64 letters, digits, or _.@-")
    if not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12-256 characters.")
    salt = secrets.token_hex(16)
    record = {"username": username, "salt": salt, "hash": password_hash(password, salt)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LoginError(ValueError):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


class DashboardAuth:
    def __init__(self, path: Path, *, secure_cookie=False):
        self.path = path
        self.secure_cookie = secure_cookie
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        self.fingerprint = None

    def _credentials(self):
        raw = self.path.read_bytes() if self.path.exists() else b""
        fingerprint = hashlib.sha256(raw).digest()
        if fingerprint != self.fingerprint:
            self.sessions.clear()
            self.fingerprint = fingerprint
        credentials = json.loads(raw) if raw else None
        if credentials is not None and (not isinstance(credentials, dict) or
                any(not isinstance(credentials.get(key), str) for key in ("username", "salt", "hash"))):
            raise ValueError("Invalid web login credentials file.")
        return credentials

    def login(self, username, password, peer):
        with self.lock:
            now = time.monotonic()
            self.attempts = {key: times for key, values in self.attempts.items()
                             if (times := [t for t in values if now - t < 900])}
            attempts = self.attempts.setdefault(peer, [])
            if len(attempts) >= 5 or sum(map(len, self.attempts.values())) >= 30:
                raise LoginError("로그인 시도가 많습니다. 15분 후 다시 시도하세요.", 429)
            attempts.append(now)
            credentials = self._credentials()
            if not credentials:
                raise LoginError("Mac에서 Setup-Web-Login.command를 먼저 실행하세요.", 503)
            if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
                raise LoginError("ID 또는 비밀번호가 일치하지 않습니다.")
            candidate = password_hash(password, credentials["salt"])
            if not (hmac.compare_digest(candidate, credentials["hash"]) and
                    hmac.compare_digest(username.encode(), credentials["username"].encode())):
                raise LoginError("ID 또는 비밀번호가 일치하지 않습니다.")
            self.attempts.pop(peer, None)
            self.sessions = {key: value for key, value in self.sessions.items()
                             if value["expires"] > now and now - value["last"] < IDLE_SECONDS}
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"username": username, "expires": now + SESSION_SECONDS, "last": now}
            return token

    @staticmethod
    def token(headers):
        try:
            cookie = SimpleCookie()
            cookie.load(headers.get("Cookie", ""))
            return cookie[COOKIE].value if COOKIE in cookie else ""
        except CookieError:
            return ""

    def authorized(self, headers):
        with self.lock:
            self._credentials()
            token = self.token(headers)
            session = self.sessions.get(token)
            now = time.monotonic()
            if not session or now >= session["expires"] or now - session["last"] >= IDLE_SECONDS:
                self.sessions.pop(token, None)
                return False
            session["last"] = now
            return True

    def logout(self, headers):
        with self.lock:
            self.sessions.pop(self.token(headers), None)

    def cookie(self, token, *, clear=False):
        value = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else SESSION_SECONDS}"
        return value + ("; Secure" if self.secure_cookie else "")


def main():
    parser = argparse.ArgumentParser(description="Register/reset the dashboard owner login locally.")
    parser.add_argument("--path", type=Path, default=Path(__file__).resolve().parents[2] / ".local/config/web_login.json")
    args = parser.parse_args()
    username = input("Login ID: ").strip()
    password = getpass.getpass("Password (12+ characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    register(args.path, username, password)
    print(f"Login saved (password hash only): {args.path}")
    print("Existing browser sessions will be invalidated. Log in again.")


if __name__ == "__main__":
    main()
