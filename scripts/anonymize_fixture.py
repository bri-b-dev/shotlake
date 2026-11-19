"""Rohdokument einer API-Quelle für ein Test-Fixture anonymisieren.

    uv run python scripts/anonymize_fixture.py ROH.json AUS.json \\
        [--replace ALT=NEU ...] [--shift-days N] [--lon-shift GRAD]

Regeln (deterministisch, gleiche Eingabe ergibt gleiche Ausgabe):
- Kennungen (24-stellige Hex-IDs, UUIDs, Base64-IDs wie bei Trackman) werden einheitlich durch
  Pseudonyme ersetzt; Verweise zwischen Feldern bleiben dadurch gültig.
- Zeitstempel (ISO 8601) werden um `--shift-days` Tage verschoben (Standard -730).
- Koordinaten werden nur in der Länge verschoben (`--lon-shift`, Standard +23 Grad). Die Breite
  bleibt, damit Distanzen und Ergebnisse gleich bleiben; der Ort ist trotzdem nicht mehr der echte.
- Adressen, Telefon, Web-Adressen und Platzkennungen werden durch Platzhalter ersetzt.
- `--replace ALT=NEU` ersetzt Begriffe in allen Texten (zum Beispiel Namen des Heimatclubs). Diese
  Begriffe gehören nicht ins Repository, deshalb gibt es sie nur als Argumente.
Das Skript prüft am Ende, dass keine Original-Kennung und kein ersetzter Begriff im Ergebnis steht.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HEX24 = re.compile(r"\b[0-9a-f]{24}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
LOOSE_ID = re.compile(r"[0-9a-f]{8,}(?:-[0-9a-f]{2,})*")  # in decodierten Base64-IDs
ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")
PLACEHOLDER = {
    "street": "Musterweg 1", "city": "Musterstadt", "zip": "00000", "phone": "",
    "url": "example.org", "igolfId": "TESTID",
}  # fmt: skip


def _pseudo(text: str, length: int) -> str:
    digest = hashlib.sha256(b"shotlake-fixture:" + text.encode()).hexdigest()
    return digest[:length]


def _pseudo_uuid(text: str) -> str:
    h = _pseudo(text, 32)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _loose(match: re.Match[str]) -> str:
    token = match.group(0)
    if "-" in token:
        return _pseudo_uuid(token)
    return _pseudo(token, len(token))


class Anonymizer:
    def __init__(self, replace: dict[str, str], shift_days: int, lon_shift: float) -> None:
        self.replace, self.delta = replace, timedelta(days=shift_days)
        self.lon_shift = lon_shift
        self.original_ids: set[str] = set()

    def _id_text(self, text: str) -> str:
        for rx in (UUID, HEX24):
            for found in rx.findall(text):
                self.original_ids.add(found)
        text = UUID.sub(lambda m: _pseudo_uuid(m.group(0)), text)
        return HEX24.sub(lambda m: _pseudo(m.group(0), 24), text)

    def _base64_id(self, text: str) -> str | None:
        """Trackman-IDs sind Base64 von `Typ\\n<ids>`; nur dann ersetzen, wenn sie so aussehen."""
        if not re.fullmatch(r"[A-Za-z0-9+/]{16,}={0,2}", text):
            return None
        try:
            raw = base64.b64decode(text, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
        if "\n" not in raw:
            return None
        kind, _, rest = raw.partition("\n")
        for token in LOOSE_ID.findall(rest):
            self.original_ids.add(token)
        new = f"{kind}\n{LOOSE_ID.sub(_loose, rest)}".encode()
        out = base64.b64encode(new).decode()
        return out if text.endswith("=") else out.rstrip("=")

    def string(self, value: str) -> str:
        decoded = self._base64_id(value)
        if decoded is not None:
            return decoded
        match = ISO.match(value)
        if match:
            head, frac, zone = match.groups()
            when = datetime.fromisoformat(head) + self.delta
            return when.strftime("%Y-%m-%dT%H:%M:%S") + (frac or "") + (zone or "")
        value = self._id_text(value)
        for old, new in self.replace.items():
            value = value.replace(old, new)
        return value

    def walk(self, node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in PLACEHOLDER and isinstance(v, str):
                    out[k] = PLACEHOLDER[k]
                elif k in ("latitude", "longitude") and isinstance(v, int | float):
                    out[k] = round(v + (self.lon_shift if k == "longitude" else 0.0), 7)
                elif k == "coordinates" and _is_lonlat(v):
                    out[k] = [round(v[0] + self.lon_shift, 7), v[1]]
                else:
                    out[self.string(k) if HEX24.fullmatch(k) else k] = self.walk(v, k)
            return out
        if isinstance(node, list):
            return [self.walk(v, key) for v in node]
        if isinstance(node, str):
            return self.string(node)
        return node


def _is_lonlat(v: Any) -> bool:
    return (
        isinstance(v, list)
        and len(v) == 2
        and all(isinstance(x, int | float) and not isinstance(x, bool) for x in v)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("target", type=Path)
    ap.add_argument("--replace", action="append", default=[], metavar="ALT=NEU")
    ap.add_argument("--shift-days", type=int, default=-730)
    ap.add_argument("--lon-shift", type=float, default=23.0)
    args = ap.parse_args(argv)
    replace = dict(item.split("=", 1) for item in args.replace)
    anon = Anonymizer(replace, args.shift_days, args.lon_shift)
    result = anon.walk(json.loads(args.source.read_text()))
    text = json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    leaks = [i for i in anon.original_ids if i in text] + [o for o in replace if o in text]
    if leaks:
        print(f"Abbruch: Originalwerte im Ergebnis ({len(leaks)})", file=sys.stderr)
        return 1
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(text)
    print(f"{args.target} ({len(text) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
