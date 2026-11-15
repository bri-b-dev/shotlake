"""Konfigurationsdateien (versioniert im Repo): Schlägerzuordnung."""

from __future__ import annotations

import json
import os
import re
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


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    """Schlüssel nur ohne Anführungszeichen schreiben, wenn TOML das erlaubt (nicht z. B. `56°`)."""
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def save_club_map(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    lines = [HEADER]
    for source in sorted(mapping):
        lines.append(f"\n[{_toml_key(source)}]")
        for ref in sorted(mapping[source], key=lambda r: (len(r), r)):
            lines.append(f'{_toml_key(ref)} = "{mapping[source][ref]}"')
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
        # Range-Schläge ohne Typ (z. B. Trackman `52Wedge`) erhalten die Nutzerzuordnung
        con.execute(
            "update shot set club_type = m.club_type, "
            "flags = list_filter(flags, lambda x: x != 'club_unmapped') "
            "from club_map m where shot.source = m.source and shot.club_raw = m.club_ref "
            "and shot.club_type is null"
        )
    apply_club_overrides(con, path.with_name("club_overrides.toml"))
    return len(rows)


def apply_club_overrides(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    """Schlägertyp für Schläge ohne Namen setzen (Angabe der Nutzerin, z. B. bei Trackman).

    Jeder `[[override]]` nennt `source`, `session` (Teil der Session-Kennung), `club` und optional
    `from`/`to` (UTC, ISO). Es werden nur Schläge ohne Typ ergänzt, nie bestehende überschrieben.
    """
    if not path.exists():
        return 0
    with path.open("rb") as fh:
        entries = tomllib.load(fh).get("override", [])
    changed = 0
    for e in entries:
        if e["club"] not in CANONICAL_CLUBS:
            raise ValueError(f"{path}: unbekannter Schlägertyp {e['club']!r}")
        where = ["source = ?", "club_type is null", "session_id like ?"]
        params: list[object] = [e["source"], f"%{e['session']}%"]
        if "from" in e:
            where.append("shot_time_utc >= ?")
            params.append(str(e["from"]))
        if "to" in e:
            where.append("shot_time_utc <= ?")
            params.append(str(e["to"]))
        row = con.execute(
            "update shot set club_type = ?, "
            "flags = list_filter(flags, lambda x: x != 'club_unmapped') "
            f"where {' and '.join(where)} returning 1",
            [e["club"], *params],
        ).fetchall()
        changed += len(row)
    return changed
