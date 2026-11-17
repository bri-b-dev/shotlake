"""Garmin-Erstanmeldung mit Zwei-Faktor-Code für die Weboberfläche.

Zweistufig: `start` meldet mit E-Mail und Passwort an. Verlangt Garmin einen Code, bleibt der
(JSON-)Anmeldezustand nur im Speicher und verfällt nach `TTL_S`; `finish` schließt mit dem Code ab.
Danach liegt das Token (`garmin_tokens.json`, 0600) im Token-Verzeichnis und der normale Sync
meldet sich damit an. E-Mail und Passwort werden nie gespeichert und stehen nie in Meldungen.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

TTL_S = 600
TOKEN_FILE = "garmin_tokens.json"


class GarminLoginError(Exception):
    """Anmeldung gescheitert; Text ist für die Anzeige geeignet (ohne Zugangsdaten)."""


def _default_factory(email: str | None, password: str | None, mfa: bool) -> Any:
    try:
        garmin_cls = import_module("garminconnect").Garmin
    except ModuleNotFoundError as exc:
        raise GarminLoginError("garminconnect nicht installiert (uv sync --extra garmin)") from exc
    if email is None:
        return garmin_cls()
    return garmin_cls(email, password, return_on_mfa=mfa)


def _explain(exc: Exception) -> GarminLoginError:
    name = type(exc).__name__
    if "TooManyRequests" in name:
        return GarminLoginError("Garmin begrenzt Anmeldungen gerade, später erneut versuchen.")
    if "Authentication" in name:
        return GarminLoginError("Anmeldung abgelehnt (E-Mail, Passwort oder Code falsch).")
    if "Connection" in name or isinstance(exc, ConnectionError | TimeoutError):
        return GarminLoginError("Garmin nicht erreichbar.")
    return GarminLoginError(f"Anmeldung fehlgeschlagen ({name}).")


class GarminLogin:
    def __init__(
        self,
        tokenstore: Path,
        factory: Callable[[str | None, str | None, bool], Any] | None = None,
        ttl: float = TTL_S,
    ) -> None:
        self.tokenstore = tokenstore
        self._factory = factory or _default_factory
        self._ttl = ttl
        self._lock = threading.Lock()
        self._state: Any = None
        self._expires = 0.0

    def tokens_present(self) -> bool:
        return (self.tokenstore / TOKEN_FILE).exists()

    def pending(self) -> bool:
        with self._lock:
            return self._state is not None and time.monotonic() < self._expires

    def cancel(self) -> None:
        with self._lock:
            self._state = None

    def _save(self, garmin: Any) -> None:
        self.tokenstore.mkdir(parents=True, exist_ok=True, mode=0o700)
        garmin.client.dump(str(self.tokenstore))

    def start(self, email: str, password: str) -> str:
        """`"ok"` (Token gespeichert) oder `"mfa"` (Code fehlt noch)."""
        try:
            garmin = self._factory(email.strip(), password, True)
            status, state = garmin.login()
            if status == "needs_mfa":
                with self._lock:
                    self._state = state
                    self._expires = time.monotonic() + self._ttl
                return "mfa"
            self._save(garmin)
        except GarminLoginError:
            raise
        except Exception as exc:
            raise _explain(exc) from None  # `from None`: keine Zugangsdaten in der Kette
        return "ok"

    def finish(self, code: str) -> None:
        with self._lock:
            state, expires = self._state, self._expires
        if state is None or time.monotonic() >= expires:
            self.cancel()
            raise GarminLoginError("Keine offene Anmeldung (abgelaufen): neu beginnen.")
        try:
            garmin = self._factory(None, None, False)
            garmin.resume_login(state, code.strip())
            self._save(garmin)
        except GarminLoginError:
            raise
        except Exception as exc:
            raise _explain(exc) from None
        self.cancel()
