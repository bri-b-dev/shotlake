#!/usr/bin/env python3
"""Zugangsdaten aus dem Login-HAR in eine lokale Env-Datei ziehen (läuft in DEINEM Terminal).

Liest den `magic-link?accessKey=…&secret=…&userId=…`-Aufruf aus dem Mitschnitt des Mail-Logins
und schreibt `secrets/arccos.env` (Rechte 0600, gitignored). Es wird nichts ausgegeben außer einer
Erfolgsmeldung. Danach: `source secrets/arccos.env` und `shotlake arccos-check`.
Später in sops übernehmen und die Env-Datei sowie das HAR löschen.

    python scripts/phase0/arccos_extract_credentials.py login2.dashboard.arccosgolf.com.har
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    har = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "/magic-link?" not in url:
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if not {"accessKey", "secret", "userId"} <= set(query):
            continue
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
        print(f"Zugangsdaten nach {target} geschrieben (0600). Nichts ausgegeben.")
        return 0
    print("Kein magic-link-Aufruf mit accessKey/secret/userId im HAR gefunden.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
