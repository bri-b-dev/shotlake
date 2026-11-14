"""Live-Zugang zur internen Web-API von BeBrassie (app.bebrassie.com/api/v1). UNGEPRÜFT live.

Ablauf nach dem Poller aus `analytics-platform` (aus einem HAR vom 2026-10-17 abgeleitet):
1. `POST /login` mit E-Mail und Passwort; die Session hält das Cookie. Ein Bearer-Token wird nur
   gesetzt, falls die Antwort eines der üblichen Token-Felder enthält (Feldname unbestätigt).
2. `GET PlayedRound?deepPopulate=course&limit&skip&sort&query`: Rundenliste, Gesamtzahl im Header
   `x-total-count`. Die Sortierung der Liste ist nicht verifiziert; deshalb wird nie anhand der
   Reihenfolge abgebrochen.
3. `GET show-round/<id>` liefert `{success, data}`. Listeneintrag und Detail werden zusammengeführt
   (Detail gewinnt), Platzgeometrie und Nutzerdaten vorher entfernt.

Zugangsdaten tauchen nie in Meldungen auf.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .api import AuthError
from .errors import SchemaDriftError
from .http import JsonTransport, UrllibTransport, raise_for_status

BASE_URL = "https://app.bebrassie.com/api/v1"
PAGE_SIZE = 50
LIST_PATH = "PlayedRound"
# gleicher Filter wie die App: signierte oder reine Score-Runden (keine laufenden)
LIST_QUERY = {"$or": [{"signedRound": True}, {"scoreOnly": True}]}
TOKEN_FIELDS = ("accessToken", "access_token", "access-token", "token")
# Pfade, die vor dem Speichern entfernt werden (1,3 MB Platzgeometrie, Klarnamen)
DROP_PATHS = [
    ("course", "courseData"),
    ("courseTee", "locations"),
    ("courseTee", "teeboxShapes"),
    ("courseTees",),
    ("ogTags",),
    ("user",),
]


def slim(data: dict[str, Any]) -> dict[str, Any]:
    """Platzgeometrie und personenbezogene Teile entfernen (arbeitet auf einer Kopie)."""
    out = json.loads(json.dumps(data))
    for path in DROP_PATHS:
        node: Any = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out  # type: ignore[no-any-return]


@dataclass
class BeBrassieClient:
    email: str
    password: str
    transport: JsonTransport = field(default_factory=UrllibTransport)
    base_url: str = BASE_URL
    lang: str = "de_DE"
    _headers: dict[str, str] = field(default_factory=dict, repr=False)
    _logged_in: bool = field(default=False, repr=False)

    def __repr__(self) -> str:  # Zugangsdaten nie in Reprs
        return f"BeBrassieClient(base_url={self.base_url!r})"

    def login(self) -> None:
        resp = self.transport.post_json(
            f"{self.base_url}/login",
            {"lang": self.lang, "email": self.email, "password": self.password},
        )
        if resp.status in (400, 401, 403, 404, 422):
            raise AuthError("BeBrassie-Anmeldung abgelehnt (E-Mail oder Passwort prüfen)")
        raise_for_status(resp)
        try:
            body = json.loads(resp.text)
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("success") is False:
            raise AuthError("BeBrassie-Anmeldung abgelehnt (E-Mail oder Passwort prüfen)")
        for candidate in (body, body.get("data") if isinstance(body, dict) else None):
            if isinstance(candidate, dict):
                for key in TOKEN_FIELDS:
                    if isinstance(candidate.get(key), str) and candidate[key]:
                        self._headers["authorization"] = f"Bearer {candidate[key]}"
        self._logged_in = True

    def _get_json(
        self,
        path: str,
        params: dict[str, str | int] | None = None,
        *,
        soft_client_errors: bool = False,
    ) -> Any:
        """GET mit JSON-Antwort. `soft_client_errors`: 4xx (außer Auth/Rate-Limit) -> (None, ...)."""
        if not self._logged_in:
            raise AuthError("Nicht angemeldet (login() zuerst)")
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        resp = self.transport.get(url, {"accept": "application/json", **self._headers})
        if soft_client_errors and 400 <= resp.status < 500 and resp.status not in (401, 403, 429):
            return None, resp.headers
        raise_for_status(resp)
        try:
            return json.loads(resp.text), resp.headers
        except ValueError:
            raise SchemaDriftError(f"{path}: keine JSON-Antwort") from None

    def list_rounds(self) -> list[dict[str, Any]]:
        """Alle Listeneinträge, seitenweise bis `x-total-count` (Reihenfolge unverifiziert)."""
        rounds: list[dict[str, Any]] = []
        skip = 0
        while True:
            body, headers = self._get_json(
                LIST_PATH,
                {
                    "deepPopulate": "course",
                    "limit": PAGE_SIZE,
                    "skip": skip,
                    "sort": json.dumps({"startedAt": -1}),
                    "query": json.dumps(LIST_QUERY),
                },
            )
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise SchemaDriftError("Rundenliste: Feld `data` (Liste) fehlt")
            batch: list[dict[str, Any]] = body["data"]
            rounds.extend(batch)
            skip += len(batch)
            total_text = headers.get("x-total-count", "")
            total = int(total_text) if total_text.isdigit() else len(rounds)
            if not batch or skip >= total:
                return rounds

    def show_round(self, round_id: str) -> dict[str, Any] | None:
        """Detaildaten einer Runde; None, wenn es für die Runde keine gibt.

        Das ist der Fall bei `success=false` und bei einem Client-Fehler (z. B. HTTP 400 bei reinen
        Score-Runden ohne Detail): die Runde bleibt dann als Listeneintrag erhalten.
        """
        body, _ = self._get_json(
            f"show-round/{urllib.parse.quote(str(round_id), safe='')}", soft_client_errors=True
        )
        if body is None:
            return None
        if not isinstance(body, dict):
            raise SchemaDriftError("show-round: JSON-Objekt erwartet")
        if not body.get("success"):
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            raise SchemaDriftError("show-round: Feld `data` fehlt")
        return data
