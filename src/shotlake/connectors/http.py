"""Gemeinsamer HTTP-Transport für Live-Connectors (Cookie-Jar, Redirects, saubere Fehler).

URLs können Zugangsdaten enthalten; Fehlermeldungen nennen daher nur die Fehlerklasse.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Protocol

from .api import AuthError, RateLimitedError, TransientError
from .errors import SchemaDriftError


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str] | None = None) -> Response: ...


class JsonTransport(Transport, Protocol):
    def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Response: ...


class UrllibTransport:
    """HTTP mit Cookie-Jar, folgt Redirects; Fehlerstatus kommen als Response zurück."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._timeout = timeout_s

    def _send(self, req: urllib.request.Request) -> Response:
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return Response(
                    resp.status,
                    resp.read().decode("utf-8", "replace"),
                    {k.lower(): v for k, v in resp.headers.items()} | {"x-final-url": resp.url},
                )
        except urllib.error.HTTPError as exc:
            return Response(
                exc.code,
                exc.read().decode("utf-8", "replace"),
                {k.lower(): v for k, v in exc.headers.items()},
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # nur die Fehlerklasse, nie die URL (kann Zugangsdaten enthalten)
            raise TransientError(f"Netzwerkfehler: {type(exc).__name__}") from None

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        return self._send(
            urllib.request.Request(url, headers={"User-Agent": "shotlake", **(headers or {})})
        )

    def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Response:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "User-Agent": "shotlake",
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(headers or {}),
            },
        )
        return self._send(req)


def raise_for_status(resp: Response) -> None:
    if resp.status in (401, 403):
        raise AuthError(f"Zugriff verweigert (HTTP {resp.status}); Session abgelaufen?")
    if resp.status == 429:
        retry = resp.headers.get("retry-after", "")
        raise RateLimitedError(float(retry) if retry.isdigit() else None)
    if resp.status >= 500:
        raise TransientError(f"Serverfehler HTTP {resp.status}")
    if resp.status >= 400:
        raise SchemaDriftError(f"Unerwartete Antwort HTTP {resp.status}")
