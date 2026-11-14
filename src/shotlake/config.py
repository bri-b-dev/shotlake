"""Konfigurationsdateien (versioniert im Repo): Schlägerzuordnung."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import duckdb

from .clubs import CANONICAL_CLUBS

HEADER = (
    "# Zuordnung quellenspezifischer Schläger auf kanonische Typen (siehe shotlake.clubs).\n"
    "# Maßgeblich: wird bei jedem Aufruf in die Datenbank übernommen;"
    " `shotlake club-set` ändert diese Datei.\n"
)


def default_sops_file() -> Path:
    return Path(os.environ.get("SHOTLAKE_SOPS_FILE", "secrets/shotlake.sops.json"))


def default_club_map_path() -> Path:
    return Path(os.environ.get("SHOTLAKE_CLUB_MAP", "config/club_map.toml"))


def load_club_map(path: Path) -> dict[str, dict[str, str]]:
    """Datei lesen; unbekannte Schlägertypen sind ein Fehler (kein stilles Raten)."""
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    result: dict[str, dict[str, str]] = {}
    for source, entries in raw.items():
        if not isinstance(entries, dict):
            raise ValueError(f"{path}: Abschnitt [{source}] erwartet")
        for ref, club in entries.items():
            if club not in CANONICAL_CLUBS:
                raise ValueError(f"{path}: unbekannter Schlägertyp {club!r} für {source}/{ref}")
        result[source] = {str(k): str(v) for k, v in entries.items()}
    return result


def save_club_map(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    lines = [HEADER]
    for source in sorted(mapping):
        lines.append(f"\n[{source}]")
        for ref in sorted(mapping[source], key=lambda r: (len(r), r)):
            lines.append(f'{ref} = "{mapping[source][ref]}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_club_map(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    """Datenbanktabelle `club_map` an die Datei angleichen (Datei ist maßgeblich).

    Existiert die Datei nicht, bleibt die Tabelle unverändert. Rückgabe: Anzahl Einträge.
    """
    if not path.exists():
        return 0
    mapping = load_club_map(path)
    con.execute("delete from club_map")
    rows = [(s, ref, club) for s, entries in mapping.items() for ref, club in entries.items()]
    if rows:
        con.executemany("insert into club_map values (?,?,?)", rows)
    return len(rows)
