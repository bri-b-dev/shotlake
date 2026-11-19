"""Passwortschutz für die Weboberfläche (eine Nutzerin, Sitzungs-Cookie, keine Zusatzpakete).

- Passwort: scrypt-Hash mit Salt in `auth.json` im Datenverzeichnis (Rechte 0600), mindestens
  10 Zeichen. Ohne gesetztes Passwort führt jeder Aufruf auf `/setup`.
- Sitzung: signiertes Cookie (HMAC-SHA256, zufälliger Schlüssel in `auth.json`), 30 Tage gültig,
  `HttpOnly`, `SameSite=Strict`, `Secure` bei HTTPS. Ändert sich das Passwort, ist der Schlüssel neu
  und alle Sitzungen enden.
- Schreibende Anfragen (POST) mit fremdem `Origin` werden abgelehnt.
- Fehlversuche beim Login werden gebremst (5 Fehlversuche, dann 60 s Sperre).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

COOKIE = "shotlake_session"
SESSION_DAYS = 30
MIN_PASSWORD = 10
MAX_FAILURES, LOCK_SECONDS = 5, 60
OPEN_PATHS = ("/static/", "/login", "/setup", "/healthz", "/readyz")


class AuthStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "auth.json"
        self.enabled = True  # install_auth setzt den tatsächlichen Zustand

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".auth.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()

    def is_set(self) -> bool:
        return "hash" in self._read()

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Passwort: mindestens {MIN_PASSWORD} Zeichen")
        salt = secrets.token_bytes(16)
        self._write(
            {
                "salt": salt.hex(),
                "hash": self._hash(password, salt),
                "session_key": secrets.token_hex(32),  # neuer Schlüssel beendet alte Sitzungen
            }
        )

    def verify(self, password: str) -> bool:
        data = self._read()
        if "hash" not in data:
            return False
        return hmac.compare_digest(self._hash(password, bytes.fromhex(data["salt"])), data["hash"])

    def _key(self) -> bytes:
        return bytes.fromhex(self._read().get("session_key", ""))

    def new_session(self) -> str:
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        body = base64.urlsafe_b64encode(f"{expires}.{secrets.token_hex(8)}".encode()).decode()
        return f"{body}.{hmac.new(self._key(), body.encode(), hashlib.sha256).hexdigest()}"

    def valid_session(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie or not self.is_set():
            return False
        body, _, sig = cookie.rpartition(".")
        good = hmac.new(self._key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        try:
            expires = int(base64.urlsafe_b64decode(body.encode()).decode().split(".")[0])
        except ValueError:
            return False
        return expires > time.time()


def _safe_next(target: str | None) -> str:
    """Nur lokale Pfade als Ziel nach dem Login (kein offener Redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def install_auth(
    app: FastAPI,
    data_dir: Path,
    render: Callable[..., HTMLResponse],
    enabled: bool,
) -> AuthStore:
    store = AuthStore(data_dir)
    store.enabled = enabled
    failures: list[float] = []

    if enabled:

        @app.middleware("http")
        async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
            path = request.url.path
            if request.method == "POST":
                origin = request.headers.get("origin")
                if origin and urlparse(origin).netloc != request.headers.get("host"):
                    return Response("Fremder Ursprung", status_code=403)
            if path.startswith(OPEN_PATHS):
                return await call_next(request)
            if not store.is_set():
                return RedirectResponse("/setup", status_code=303)
            if not store.valid_session(request.cookies.get(COOKIE)):
                return RedirectResponse(f"/login?next={_safe_next(path)}", status_code=303)
            return await call_next(request)

    def start_session(request: Request, target: str) -> RedirectResponse:
        response = RedirectResponse(_safe_next(target), status_code=303)
        response.set_cookie(
            COOKIE,
            store.new_session(),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request) -> Response:
        if store.is_set():
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html", error=None, min_length=MIN_PASSWORD)

    @app.post("/setup")
    async def setup_submit(request: Request) -> Response:
        if store.is_set():
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        password, repeat = str(form.get("password", "")), str(form.get("repeat", ""))
        error = None
        if password != repeat:
            error = "Die Passwörter stimmen nicht überein."
        else:
            try:
                store.set_password(password)
            except ValueError as exc:
                error = str(exc)
        if error:
            response = render(request, "setup.html", error=error, min_length=MIN_PASSWORD)
            response.status_code = 400
            return response
        return start_session(request, "/")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if not store.is_set():
            return RedirectResponse("/setup", status_code=303)
        return render(
            request, "login.html", error=None, next=_safe_next(request.query_params.get("next"))
        )

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        target = _safe_next(str(form.get("next", "/")))
        now = time.time()
        failures[:] = [t for t in failures if now - t < LOCK_SECONDS]
        if len(failures) >= MAX_FAILURES:
            response = render(
                request,
                "login.html",
                error="Zu viele Fehlversuche, bitte kurz warten.",
                next=target,
            )
            response.status_code = 429
            return response
        if store.verify(str(form.get("password", ""))):
            failures.clear()
            return start_session(request, target)
        failures.append(now)
        response = render(request, "login.html", error="Passwort falsch.", next=target)
        response.status_code = 401
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    return store
