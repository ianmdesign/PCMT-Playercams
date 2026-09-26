"""Browser login for producer controls, using the Spectra organization code."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response


SESSION_SECONDS = 30 * 24 * 60 * 60
ATTEMPT_WINDOW = 60
MAX_ATTEMPTS = 10


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AdminAuth:
    def __init__(self, data_dir: str | Path, cookie_name: str) -> None:
        self.cookie_name = cookie_name
        key_path = Path(data_dir) / "admin-session.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(secrets.token_bytes(32))
        self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise RuntimeError("Invalid admin session key in the data directory")
        self._failed: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _code() -> str:
        return os.getenv("SPECTRA_ORGANIZATION_CODE", "").strip()

    def _version(self, code: str) -> str:
        return _b64(hmac.new(self._key, b"org-code:" + code.encode(), hashlib.sha256).digest()[:16])

    def _state(self, cookie: str | None) -> dict[str, Any] | None:
        code = self._code()
        if not cookie or not code or len(cookie) > 4096:
            return None
        try:
            body, signature = cookie.split(".")
            raw = _decode(body)
            if not hmac.compare_digest(_decode(signature), hmac.new(self._key, raw, hashlib.sha256).digest()):
                return None
            state = json.loads(raw)
            if (
                not isinstance(state, dict)
                or type(state.get("exp")) is not int
                or state["exp"] <= time.time()
                or not isinstance(state.get("csrf"), str)
                or not hmac.compare_digest(str(state.get("version", "")), self._version(code))
            ):
                return None
            return state
        except (ValueError, TypeError, UnicodeDecodeError, KeyError, binascii.Error):
            return None

    def status(self, request: Request) -> dict[str, Any]:
        state = self._state(request.cookies.get(self.cookie_name))
        return {"authenticated": True, "csrfToken": state["csrf"]} if state else {"authenticated": False}

    def authenticated(self, request: Request) -> bool:
        return self._state(request.cookies.get(self.cookie_name)) is not None

    def authenticated_cookie(self, cookie: str | None) -> bool:
        return self._state(cookie) is not None

    def require(self, request: Request) -> dict[str, Any]:
        state = self._state(request.cookies.get(self.cookie_name))
        if not state:
            raise HTTPException(status_code=401, detail="Sign in with the Spectra organization code")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            supplied = request.headers.get("x-admin-csrf", "")
            if not supplied or not hmac.compare_digest(supplied, state["csrf"]):
                raise HTTPException(status_code=403, detail="Refresh the admin page and try again")
        return state

    def login(self, request: Request, response: Response, password: str) -> dict[str, Any]:
        # The browser may speak HTTP to a local development server. Public
        # logins must originate from the HTTPS site, even behind a TLS proxy.
        if request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            expected_origin = f"https://{request.headers.get('host', '')}"
            if request.headers.get("origin") != expected_origin:
                raise HTTPException(status_code=403, detail="Open this site using HTTPS before signing in")
        code = self._code()
        if not code:
            raise HTTPException(status_code=503, detail="Set SPECTRA_ORGANIZATION_CODE on this server")
        client = request.client.host if request.client else "unknown"
        now = time.time()
        with self._lock:
            failures = self._failed[client]
            while failures and failures[0] < now - ATTEMPT_WINDOW:
                failures.popleft()
            if len(failures) >= MAX_ATTEMPTS:
                raise HTTPException(status_code=429, detail="Too many attempts; try again in a minute")
        valid = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(code.encode()).digest())
        if not valid:
            with self._lock:
                self._failed[client].append(now)
            raise HTTPException(status_code=401, detail="Incorrect organization code")
        with self._lock:
            self._failed.pop(client, None)
        state = {
            "exp": int(now) + SESSION_SECONDS,
            "csrf": secrets.token_urlsafe(32),
            "version": self._version(code),
        }
        raw = json.dumps(state, separators=(",", ":")).encode()
        cookie = f"{_b64(raw)}.{_b64(hmac.new(self._key, raw, hashlib.sha256).digest())}"
        response.set_cookie(
            self.cookie_name,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https" or request.url.hostname not in ("localhost", "127.0.0.1"),
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True, "csrfToken": state["csrf"]}

    def logout(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
