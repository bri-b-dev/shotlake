"""Live-Zugang zum Arccos-Dashboard (SvelteKit Remote Functions). UNGEPRÜFT gegen Live-Server.

Ablauf (aus HAR-Mitschnitten abgeleitet, Annahmen sind markiert):
1. Session: `GET /magic-link?accessKey&secret&userId` (Redirects folgen, Cookies behalten).
   ANNAHME: Die beim Mail-Login erhaltenen Credentials lassen sich wiederverwenden.
2. Funktions-Hashes: stehen als `` t(`<hash>/<funktion>`) `` in kleinen JS-Chunks; sie werden zur
   Laufzeit aus dem Frontend-Code gelesen (der Hash ändert sich mit dem Build).
3. Aufrufe: `GET /_app/remote/<hash>/<funktion>?payload=<base64url>`, Antwort
   `{"type":"result","data":"<devalue>"}`; das Ergebnis steckt unter `_`.
4. Jahre über `roundYears`, Runden je Jahr über `roundsBundle`, Details über `roundDetailBundle`.

Zugangsdaten tauchen nie in Fehlermeldungen oder Logs auf; Anmelde-URLs werden nicht ausgegeben.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Protocol

from ..devalue import DevalueError, encode_remote_arg
from ..devalue import parse as devalue_parse
from .api import AuthError, RateLimitedError, TransientError
from .errors import SchemaDriftError

BASE_URL = "https://dashboard.arccosgolf.com"
GOAL_HCP = -20  # Wert, den das Dashboard in allen Aufrufen sendet (Bedeutung ungeklärt)
ROUNDS_LIMIT = 50
MAX_JS_FILES = 250
_HASH = re.compile(r"[`\"']([a-z0-9]{4,10})/(\w+)[`\"']")
_IMPORT = re.compile(r"""["'](\.{0,2}/[^"'\s]+?\.js)["']""")
_ASSET = re.compile(r"""["'(](/_app/immutable/[^"'\s)]+?\.js)["')]""")


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str] | None = None) -> Response: ...


class UrllibTransport:
    """HTTP mit Cookie-Jar, folgt Redirects; Fehlerstatus kommen als Response zurück."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self.jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._timeout = timeout_s

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        req = urllib.request.Request(url, headers={"User-Agent": "shotlake", **(headers or {})})
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return Response(
                    resp.status,
                    resp.read().decode("utf-8", "replace"),
                    {k.lower(): v for k, v in resp.headers.items()} | {"x-final-url": resp.url},
                )
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read().decode("utf-8", "replace"),
                            {k.lower(): v for k, v in exc.headers.items()})  # fmt: skip
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # nur die Fehlerklasse, nie die URL (kann Zugangsdaten enthalten)
            raise TransientError(f"Netzwerkfehler: {type(exc).__name__}") from None


def _raise_for_status(resp: Response) -> None:
    if resp.status in (401, 403):
        raise AuthError(f"Zugriff verweigert (HTTP {resp.status}); Session abgelaufen?")
    if resp.status == 429:
        retry = resp.headers.get("retry-after", "")
        raise RateLimitedError(float(retry) if retry.isdigit() else None)
    if resp.status >= 500:
        raise TransientError(f"Serverfehler HTTP {resp.status}")
    if resp.status >= 400:
        raise SchemaDriftError(f"Unerwartete Antwort HTTP {resp.status}")


def discover_remote_hashes(
    transport: Transport, base_url: str, names: set[str], max_files: int = MAX_JS_FILES
) -> dict[str, str]:
    """Funktions-Hashes aus dem Frontend-Code lesen (Breitensuche ab der Startseite)."""
    start = transport.get(base_url + "/")
    _raise_for_status(start)
    queue: deque[str] = deque(
        urllib.parse.urljoin(base_url + "/", m) for m in dict.fromkeys(_ASSET.findall(start.text))
    )
    seen: set[str] = set()
    found: dict[str, str] = {}
    while queue and len(seen) < max_files and not names <= set(found):
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        resp = transport.get(url)
        if resp.status != 200:
            continue
        for hash_, name in _HASH.findall(resp.text):
            if name in names and name not in found:
                found[name] = hash_
        for ref in _IMPORT.findall(resp.text):
            nxt = urllib.parse.urljoin(url, ref)
            if "/_app/immutable/" in nxt and nxt not in seen:
                queue.append(nxt)
    missing = names - set(found)
    if missing:
        raise SchemaDriftError(f"Funktions-Hashes nicht gefunden: {sorted(missing)}")
    return found


@dataclass
class LiveArccosClient:
    """Implementiert `ArccosClient` gegen das Live-Dashboard."""

    user_id: str
    access_key: str
    secret: str
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = BASE_URL
    _hashes: dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # Zugangsdaten nie in Reprs
        return f"LiveArccosClient(base_url={self.base_url!r})"

    def login(self) -> None:
        query = urllib.parse.urlencode(
            {"accessKey": self.access_key, "secret": self.secret, "userId": self.user_id}
        )
        resp = self.transport.get(f"{self.base_url}/magic-link?{query}")
        if resp.status in (401, 403, 404) or "/login" in resp.headers.get("x-final-url", ""):
            raise AuthError("Anmeldung mit gespeicherten Zugangsdaten abgelehnt (neu einloggen)")
        _raise_for_status(resp)
        self._hashes = discover_remote_hashes(
            self.transport, self.base_url, {"roundsBundle", "roundDetailBundle", "roundYears"}
        )

    def _call(self, name: str, args: dict[str, str | int | float | bool | None]) -> Any:
        if not self._hashes:
            raise AuthError("Nicht angemeldet (login() zuerst)")
        payload = encode_remote_arg(args)
        url = f"{self.base_url}/_app/remote/{self._hashes[name]}/{name}?payload={payload}"
        resp = self.transport.get(
            url, {"x-sveltekit-pathname": "/", "x-sveltekit-search": "", "accept": "*/*"}
        )
        _raise_for_status(resp)
        try:
            outer = json.loads(resp.text)
            if outer.get("type") != "result":
                raise SchemaDriftError(f"{name}: Antworttyp {outer.get('type')!r}")
            return devalue_parse(outer["data"])["_"]
        except (KeyError, TypeError, ValueError, DevalueError) as exc:
            raise SchemaDriftError(f"{name}: Antwort nicht lesbar ({type(exc).__name__})") from None

    def _years(self) -> list[int]:
        result = self._call(
            "roundYears", {"includeExcluded": True, "subjectUserId": "", "userId": self.user_id}
        )
        if not isinstance(result, list):
            raise SchemaDriftError("roundYears: Liste erwartet")
        return [int(y) for y in result]

    def rounds(self) -> list[dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        for year in self._years():
            limit = ROUNDS_LIMIT
            while True:
                result = self._call(
                    "roundsBundle",
                    {
                        "goalHcp": GOAL_HCP, "includeExcluded": True, "limit": limit,
                        "subjectUserId": "", "tournamentOnly": False,
                        "userId": self.user_id, "year": year,
                    },
                )  # fmt: skip
                data = _need(result, "list", "data")
                for row in data["rounds"]:
                    rows[int(row["roundId"])] = row
                if not data.get("hasMore") or limit >= 1000:
                    break
                limit *= 4
        return sorted(rows.values(), key=lambda r: str(r.get("startTime")), reverse=True)

    def round_detail(self, round_id: int) -> dict[str, Any] | None:
        result = self._call(
            "roundDetailBundle",
            {"goalHcp": GOAL_HCP, "roundId": str(round_id), "subjectUserId": "", "variant": "cold"},
        )
        return _need(result, "bundle", "data")  # type: ignore[no-any-return]


def _need(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise SchemaDriftError(f"Antwort ohne {'.'.join(path)}")
        cur = cur[key]
    return cur
