#!/usr/bin/env python3
"""BeBrassie-Zugangsdaten in eine lokale Env-Datei schreiben (läuft in DEINEM Terminal).

Fragt E-Mail und Passwort ab (Passwort verdeckt) und schreibt `secrets/bebrassie.env` mit Rechten
0600 (gitignored). Es wird nichts ausgegeben. Danach: `source secrets/bebrassie.env`. Später in sops
übernehmen und die Env-Datei löschen.

    python scripts/phase0/bebrassie_set_credentials.py
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path


def quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    email = input("BeBrassie E-Mail: ").strip()
    password = getpass.getpass("BeBrassie Passwort (verdeckt): ")
    if not email or not password:
        print("E-Mail und Passwort sind nötig.")
        return 1
    out = Path("secrets")
    out.mkdir(mode=0o700, exist_ok=True)
    target = out / "bebrassie.env"
    lines = [
        f"export SHOTLAKE_BEBRASSIE_EMAIL={quote(email)}",
        f"export SHOTLAKE_BEBRASSIE_PASSWORD={quote(password)}",
    ]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Zugangsdaten nach {target} geschrieben (0600). Nichts ausgegeben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
