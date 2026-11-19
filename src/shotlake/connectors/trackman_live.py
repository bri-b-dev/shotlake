"""Live-Zugang zur Trackman-GraphQL-API (`api.trackmangolf.com/graphql`). UNGEPRÜFT live.

Der Web-Client von Trackman ist vertraulich (kein eigener OAuth-Austausch möglich). Shotlake nutzt
deshalb ein Bearer-Token aus einer Browser-Anmeldung (ca. 7 Tage gültig):
1. `SHOTLAKE_TRACKMAN_TOKEN` in der Umgebung (bzw. sops-Eintrag `trackman.token`), sonst
2. die Token-Datei des `golf-coach`-Plugins (`~/.golf-coach/token.json`, Feld `access_token`).
Läuft das Token ab, kommt `AuthError`; das Token steht nie in Meldungen.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import trackman_queries as q
from .api import AuthError, RetryPolicy, TransientError, load_secret, with_retry
from .errors import SchemaDriftError
from .http import JsonTransport, UrllibTransport, raise_for_status

ENDPOINT = "https://api.trackmangolf.com/graphql"
PAGE_SIZE = 50
SCORECARD_PAGE = 10  # Runden mit allen Löchern und Schlägen sind schwere Antworten
TIMEOUT_S = 90.0
SERVER_ERRORS = ("unexpected execution error", "internal server error", "service unavailable")
SKEW_S = 120


def _expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return int(exp) if isinstance(exp, int | float) else None


def _plugin_token_file() -> Path:
    base = os.environ.get("GOLF_COACH_CACHE_DIR")
    return (Path(base) if base else Path.home() / ".golf-coach") / "token.json"


def load_token(sops_file: Path | None = None) -> str:
    """Token aus Umgebung/sops, sonst aus der Plugin-Datei; abgelaufene Token werden abgelehnt."""
    token: str | None = None
    try:
        token = load_secret("trackman", "token", sops_file)
    except AuthError:
        path = _plugin_token_file()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError:
                data = {}
            value = data.get("access_token") if isinstance(data, dict) else None
            token = value if isinstance(value, str) else None
    if not token:
        raise AuthError(
            "Kein Trackman-Token: SHOTLAKE_TRACKMAN_TOKEN setzen oder im golf-coach-Plugin anmelden"
        )
    token = token.strip().removeprefix("Bearer ").strip()
    exp = _expiry(token)
    if exp is not None and time.time() >= exp - SKEW_S:
        raise AuthError("Trackman-Token abgelaufen: im Browser neu anmelden (golf-coach `auth`)")
    return token


class ServerError(TransientError):
    """Interner Fehler des Trackman-Servers (kein Problem von Anmeldung oder Abfrage)."""


def _describe(errors: list[Any]) -> str:
    """Kurzfassung der GraphQL-Fehler: Text, Pfad und Code (nichts Persönliches)."""
    parts: list[str] = []
    for e in errors[:3]:
        if not isinstance(e, dict):
            continue
        text = str(e.get("message", "?"))[:120]
        path = ".".join(str(p) for p in e.get("path") or [])
        ext = e.get("extensions")
        code = str(ext.get("code")) if isinstance(ext, dict) and ext.get("code") else ""
        parts.append(
            " ".join(
                x
                for x in (text, f"(Pfad {path})" if path else "", f"[{code}]" if code else "")
                if x
            )
        )
    return "; ".join(parts)


def _is_server_error(errors: list[Any]) -> bool:
    for e in errors:
        if not isinstance(e, dict):
            continue
        ext = e.get("extensions")
        code = str(ext.get("code", "")) if isinstance(ext, dict) else ""
        if code == "INTERNAL_SERVER_ERROR" or any(
            t in str(e.get("message", "")).lower() for t in SERVER_ERRORS
        ):
            return True
    return False


@dataclass
class TrackmanClient:
    token: str
    transport: JsonTransport = field(default_factory=lambda: UrllibTransport(TIMEOUT_S))
    endpoint: str = ENDPOINT
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=3))
    sleep: Callable[[float], None] = time.sleep
    notes: list[str] = field(default_factory=list)  # Hinweise des Laufs (übersprungene Daten)
    skipped_scorecards: int = 0

    def __repr__(self) -> str:  # Token nie in Reprs
        return f"TrackmanClient(endpoint={self.endpoint!r})"

    def _query(
        self, op: str, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Abfrage `op`; Serverfehler werden wiederholt, danach als `ServerError` gemeldet."""
        return with_retry(lambda: self._query_once(op, query, variables), self.retry, self.sleep)

    def _query_once(self, op: str, query: str, variables: dict[str, Any] | None) -> dict[str, Any]:
        resp = self.transport.post_json(
            self.endpoint,
            {"query": query, "variables": variables or {}},
            {"Authorization": f"Bearer {self.token}"},
        )
        raise_for_status(resp)
        try:
            body = json.loads(resp.text)
        except ValueError:
            raise SchemaDriftError(f"Trackman {op}: keine JSON-Antwort") from None
        if not isinstance(body, dict):
            raise SchemaDriftError(f"Trackman {op}: Antwort kein Objekt")
        errors = body.get("errors")
        if errors:
            items = [e for e in errors if isinstance(e, dict)]
            messages = [str(e.get("message", "?")) for e in items]
            if any("authoriz" in m.lower() or "authenticat" in m.lower() for m in messages):
                raise AuthError("Trackman: nicht angemeldet (Token ungültig)")
            if _is_server_error(items):
                raise ServerError(f"Trackman {op}: Serverfehler {_describe(items)}")
            raise SchemaDriftError(f"Trackman {op}: GraphQL-Fehler {_describe(items)}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise SchemaDriftError(f"Trackman {op}: Antwort ohne data")
        return data

    def check(self) -> None:
        """Token prüfen (eine kleine Abfrage); `AuthError` bei ungültigem Token."""
        data = self._query("Who", q.PROFILE_CHECK)
        if not isinstance(data.get("me"), dict):
            raise AuthError("Trackman: Token nicht akzeptiert (me fehlt)")

    def list_activities(self, since: datetime | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0
        while True:
            data = self._query(
                "Activities",
                q.LIST_ACTIVITIES,
                {
                    "kinds": list(q.RANGE_KINDS),
                    "from": since.isoformat() if since else None,
                    "skip": skip,
                    "take": PAGE_SIZE,
                },
            )
            try:
                page = data["me"]["activities"]
                batch = page["items"]
            except (KeyError, TypeError):
                raise SchemaDriftError("Trackman: activities ohne items") from None
            items.extend(i for i in batch if isinstance(i, dict) and "id" in i)
            skip += PAGE_SIZE
            if not batch or skip >= int(page.get("totalCount") or 0):
                return items

    def session_detail(self, activity_id: str) -> dict[str, Any] | None:
        data = self._query("Session", q.SESSION_DETAIL, {"id": activity_id})
        node = data.get("node")
        if not isinstance(node, dict) or not isinstance(node.get("strokes"), list):
            return None  # Art ohne Schlagfragment
        return {k: v for k, v in node.items() if k in ("time", "strokes")}

    def _scorecard_page(self, skip: int, take: int) -> tuple[list[dict[str, Any]], bool]:
        """Eine Seite Scorecards; (Einträge, Ende erreicht). Bei einem Serverfehler wird die Seite
        in Einzelabfragen zerlegt, damit ein fehlerhafter Datensatz die übrigen nicht blockiert."""
        try:
            data = self._query("Scorecards", q.SCORECARDS, {"skip": skip, "take": take})
        except ServerError as exc:
            if take == 1:
                self.skipped_scorecards += 1
                self.notes.append(
                    f"Scorecard an Position {skip} übersprungen, Trackman meldet einen "
                    f"Serverfehler: {str(exc)[:200]}"
                )
                return [], False
            items: list[dict[str, Any]] = []
            for offset in range(take):
                part, exhausted = self._scorecard_page(skip + offset, 1)
                items.extend(part)
                if exhausted:
                    return items, True
            return items, False
        try:
            batch = data["me"]["scorecards"] or []
        except (KeyError, TypeError):
            raise SchemaDriftError("Trackman: scorecards fehlt") from None
        cards = [c for c in batch if isinstance(c, dict) and "id" in c]
        return cards, len(batch) < take

    def scorecards(self) -> list[dict[str, Any]]:
        self.skipped_scorecards = 0
        cards: list[dict[str, Any]] = []
        skip = 0
        while True:
            page, exhausted = self._scorecard_page(skip, SCORECARD_PAGE)
            cards.extend(page)
            skip += SCORECARD_PAGE
            if exhausted:
                return cards
