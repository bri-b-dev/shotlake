#!/usr/bin/env python3
"""Phase-0-Probe: Welche Struktur liefert die BeBrassie-Web-API? (läuft in DEINEM Terminal)

Gibt NUR die Struktur aus (Schlüssel, Typen, Listenlängen, Wertemengen unkritischer Felder),
keine Werte. Rohantworten (ohne Platzgeometrie und Nutzerdaten) können mit --save lokal nach
./probe-output/ geschrieben werden (gitignored, enthalten personenbezogene Daten).

Zugangsdaten: Umgebung `SHOTLAKE_BEBRASSIE_EMAIL` und `SHOTLAKE_BEBRASSIE_PASSWORD`, sonst
Abfrage (Passwort verdeckt, wird nicht gespeichert).

    ! uv run python scripts/phase0/bebrassie_probe.py [--save] [--rounds 3]
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from shotlake.connectors.api import AuthError, TransientError
from shotlake.connectors.bebrassie_live import BeBrassieClient, slim
from shotlake.connectors.errors import SchemaDriftError


def shape(obj: Any, depth: int = 0, max_depth: int = 7) -> Any:
    if depth > max_depth:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [] if not obj else [shape(obj[0], depth + 1, max_depth), f"x{len(obj)}"]
    return type(obj).__name__


def values_of(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(r.get(key)) for r in rows})[:12]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--rounds", type=int, default=2, help="so viele Detailantworten untersuchen")
    args = ap.parse_args()
    email = os.environ.get("SHOTLAKE_BEBRASSIE_EMAIL") or input("BeBrassie E-Mail: ").strip()
    password = os.environ.get("SHOTLAKE_BEBRASSIE_PASSWORD") or getpass.getpass("Passwort: ")
    client = BeBrassieClient(email, password)
    try:
        client.login()
        print("Login ok.")
        rows = client.list_rounds()
    except (AuthError, TransientError, SchemaDriftError) as exc:
        print(f"FEHLER {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"\n=== Rundenliste: {len(rows)} Runden")
    print(json.dumps(shape(rows[0] if rows else {}), indent=1, ensure_ascii=False)[:6000])
    for key in ("roundType", "scoreOnly", "signedRound", "proPlayerType", "usedDevice"):
        print(f"  {key}: {values_of(rows, key)}")
    newest_first = bool(rows) and rows[0].get("startedAt", "") >= rows[-1].get("startedAt", "")
    print("  startedAt-Reihenfolge:", "neueste zuerst" if newest_first else "unklar")
    out = Path("probe-output")
    if args.save:
        out.mkdir(mode=0o700, exist_ok=True)
    kinds = Counter(str(r.get("scoreOnly")) for r in rows)
    print(f"  scoreOnly-Verteilung: {dict(kinds)}")
    detailed = [r for r in rows if not r.get("scoreOnly")][: args.rounds] or rows[: args.rounds]
    for item in detailed:
        rid = str(item.get("_id"))
        try:
            data = client.show_round(rid)
        except (AuthError, TransientError, SchemaDriftError) as exc:
            print(f"\n=== show-round (Runde nicht angezeigt): FEHLER {type(exc).__name__}: {exc}")
            continue
        if data is None:
            print("\n=== show-round: success=false")
            continue
        data = slim({**item, **data})
        print("\n=== show-round (Struktur, Platzgeometrie entfernt)")
        print(json.dumps(shape(data), indent=1, ensure_ascii=False)[:14000])
        for name in ("playedHoles", "shots"):
            part = data.get(name)
            if isinstance(part, list) and part and isinstance(part[0], dict):
                print(f"  {name}: {len(part)} Einträge; Schlüssel {sorted(part[0])}")
        if args.save:
            (out / f"bebrassie_round_{rid[:8]}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
