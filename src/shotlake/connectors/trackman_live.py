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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import trackman_queries as q
from .api import AuthError, load_secret
from .errors import SchemaDriftError
from .http import JsonTransport, UrllibTransport, raise_for_status

ENDPOINT = "https://api.trackmangolf.com/graphql"
PAGE_SIZE = 50
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


@dataclass
class TrackmanClient:
    token: str
    transport: JsonTransport = field(default_factory=UrllibTransport)
    endpoint: str = ENDPOINT

    def __repr__(self) -> str:  # Token nie in Reprs
        return f"TrackmanClient(endpoint={self.endpoint!r})"

    def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.transport.post_json(
            self.endpoint,
            {"query": query, "variables": variables or {}},
            {"Authorization": f"Bearer {self.token}"},
        )
        raise_for_status(resp)
        try:
            body = json.loads(resp.text)
        except ValueError:
            raise SchemaDriftError("Trackman: keine JSON-Antwort") from None
        if not isinstance(body, dict):
            raise SchemaDriftError("Trackman: Antwort kein Objekt")
        errors = body.get("errors")
        if errors:
            messages = [str(e.get("message", "?"))[:200] for e in errors if isinstance(e, dict)]
            if any("authoriz" in m.lower() or "authenticat" in m.lower() for m in messages):
                raise AuthError("Trackman: nicht angemeldet (Token ungültig)")
            raise SchemaDriftError(f"Trackman-GraphQL-Fehler: {messages[:3]}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise SchemaDriftError("Trackman: Antwort ohne data")
        return data

    def check(self) -> None:
        """Token prüfen (eine kleine Abfrage); `AuthError` bei ungültigem Token."""
        data = self._query(q.PROFILE_CHECK)
        if not isinstance(data.get("me"), dict):
            raise AuthError("Trackman: Token nicht akzeptiert (me fehlt)")

    def list_activities(self, since: datetime | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0
        while True:
            data = self._query(
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
        data = self._query(q.SESSION_DETAIL, {"id": activity_id})
        node = data.get("node")
        if not isinstance(node, dict) or not isinstance(node.get("strokes"), list):
            return None  # Art ohne Schlagfragment
        return {k: v for k, v in node.items() if k in ("time", "strokes")}

    def scorecards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        skip = 0
        while True:
            data = self._query(q.SCORECARDS, {"skip": skip, "take": PAGE_SIZE})
            try:
                batch = data["me"]["scorecards"] or []
            except (KeyError, TypeError):
                raise SchemaDriftError("Trackman: scorecards fehlt") from None
            cards.extend(c for c in batch if isinstance(c, dict) and "id" in c)
            skip += PAGE_SIZE
            if len(batch) < PAGE_SIZE:
                return cards
