#!/usr/bin/env python3
"""Arccos-Zugangsdaten in eine lokale Env-Datei schreiben (läuft in DEINEM Terminal).

Quelle ist der Aufruf `…/magic-link?accessKey=…&secret=…&userId=…`, den der Browser beim Klick auf
den Link in der Login-Mail macht (Redirect-Kette, siehe Anleitung unten). Geschrieben wird
`secrets/arccos.env` (Rechte 0600, gitignored). Ausgegeben wird nichts außer einer Erfolgsmeldung.
Danach: `source secrets/arccos.env` und `shotlake arccos-check`.

Zwei Wege:
  1. URL einfügen (verdeckte Eingabe, landet nicht in der Shell-History):
         python scripts/phase0/arccos_extract_credentials.py
  2. aus einem HAR-Mitschnitt des Logins lesen:
         python scripts/phase0/arccos_extract_credentials.py login.har

URL aus dem Browser holen (Chrome/Edge; Firefox analog):
  - Entwicklertools öffnen (Cmd+Opt+I), Reiter „Netzwerk", „Log beibehalten" einschalten.
  - Im Dashboard einloggen: E-Mail eingeben, den Link aus der Mail im SELBEN Tab öffnen.
  - Im Netzwerk-Log nach „magic-link" filtern, den Eintrag mit `accessKey=` in der URL wählen,
    Rechtsklick -> Kopieren -> „Link-Adresse kopieren" bzw. URL kopieren, und beim Skript einfügen.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.parse
from pathlib import Path


def write_env(query: dict[str, list[str]]) -> Path:
    out = Path("secrets")
    out.mkdir(mode=0o700, exist_ok=True)
    target = out / "arccos.env"
    lines = [
        f"export SHOTLAKE_ARCCOS_ACCESS_KEY='{query['accessKey'][0]}'",
        f"export SHOTLAKE_ARCCOS_SECRET='{query['secret'][0]}'",
        f"export SHOTLAKE_ARCCOS_USER_ID='{query['userId'][0]}'",
    ]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


def credentials_from_url(url: str) -> dict[str, list[str]] | None:
    if "/magic-link" not in url:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url.strip()).query)
    return query if {"accessKey", "secret", "userId"} <= set(query) else None


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        print(__doc__)
        return 2
    query: dict[str, list[str]] | None = None
    if len(sys.argv) == 1:
        query = credentials_from_url(getpass.getpass("magic-link-URL (verdeckte Eingabe): "))
    else:
        har = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        for entry in har["log"]["entries"]:
            query = credentials_from_url(entry["request"]["url"])
            if query:
                break
    if not query:
        print("Keine magic-link-URL mit accessKey, secret und userId gefunden.", file=sys.stderr)
        return 1
    print(f"Zugangsdaten nach {write_env(query)} geschrieben (0600). Nichts ausgegeben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
