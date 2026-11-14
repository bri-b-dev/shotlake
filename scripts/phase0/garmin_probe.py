#!/usr/bin/env python3
"""Phase-0-Probe: Welche Golf-Daten liefert Garmin Connect (python-garminconnect)?

Gibt NUR die Struktur (Schluessel, Typen, Listenlaengen) aus, keine Werte.
Rohantworten werden optional lokal nach ./probe-output/ geschrieben (gitignored,
enthalten personenbezogene Daten; vor dem Weitergeben anonymisieren).

Aufruf (Python >= 3.12):
    ! uvx --from garminconnect --with curl_cffi python scripts/phase0/garmin_probe.py
Optionen:
    --save      Rohantworten nach ./probe-output/ schreiben
    --email X   E-Mail vorgeben (sonst Abfrage)

Das Passwort wird per getpass abgefragt und nie gespeichert; die Tokens landen
im Token-Store (Standard ~/.garminconnect, Rechte 0600) -- wie ein Passwort behandeln.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

try:
    from garminconnect import Garmin
except ImportError:  # pragma: no cover
    sys.exit("garminconnect fehlt: uvx --from garminconnect --with curl_cffi python <script>")


def shape(obj, depth: int = 0, max_depth: int = 6):
    """Struktur ohne Werte."""
    if depth > max_depth:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        return [shape(obj[0], depth + 1, max_depth), f"x{len(obj)}"]
    return type(obj).__name__


def find_first_id(obj):
    """Erste plausible Scorecard-ID in der Summary-Antwort suchen."""
    keys = ("scorecardId", "id", "scorecard_id")
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, int):
                return v
        for v in obj.values():
            r = find_first_id(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_first_id(v)
            if r is not None:
                return r
    return None


def prompt_mfa() -> str:
    return input("Garmin MFA-Code: ").strip()


def probe(name, fn, save_dir: Path | None):
    print(f"\n=== {name}")
    try:
        data = fn()
    except Exception as exc:  # Fehlerklasse zeigen, keine Antwortinhalte
        print(f"FEHLER: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    structure = json.dumps(shape(data), indent=2, ensure_ascii=False)
    print(structure[:6000])
    if save_dir is not None:
        save_dir.mkdir(mode=0o700, exist_ok=True)
        (save_dir / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
        # Struktur ohne Werte separat, damit sie ohne Personenbezug geteilt werden kann
        (save_dir / f"{name}.shape.json").write_text(structure)
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--email")
    ap.add_argument("--tokenstore", default=os.path.expanduser("~/.garminconnect"))
    args = ap.parse_args()

    email = args.email or input("Garmin E-Mail: ").strip()
    password = getpass.getpass("Garmin Passwort (wird nicht angezeigt/gespeichert): ")

    api = Garmin(email, password, prompt_mfa=prompt_mfa)
    try:
        api.login(args.tokenstore)
    except TypeError:
        api.login()  # aeltere/neuere Signatur
    print("Login ok.")

    out = Path("probe-output") if args.save else None
    summary = probe("golf_summary", lambda: api.get_golf_summary(0, 5), out)
    probe("golf_user_stats", lambda: api.get_golf_user_stats(), out)
    probe("golf_club_stats", lambda: api.get_golf_club_stats(50), out)

    sid = find_first_id(summary)
    if sid is None:
        print("\nKeine Scorecard-ID gefunden; Detailabrufe uebersprungen.")
        return 0
    print("\nScorecard-ID gefunden (Wert nicht angezeigt), rufe Details ab.")
    probe("golf_scorecard", lambda: api.get_golf_scorecard(sid), out)
    probe("golf_shot_data", lambda: api.get_golf_shot_data(sid), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
