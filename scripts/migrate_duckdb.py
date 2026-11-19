"""Datenbank in das Dateiformat von DuckDB 1.0 kopieren (lesbar mit jeder DuckDB-Version ab 1.0).

Nötig, wenn eine Datenbank mit DuckDB 2.x geschrieben wurde: DuckDB 1.x öffnet sie nicht
("created with a newer version of DuckDB"). Shotlake nutzt DuckDB 1.x, weil 2.x auf Prozessoren
ohne AVX2 (viele NAS-Geräte) abstürzt. Das Skript braucht deshalb die DuckDB-Version, die die Datei
geschrieben hat, und läuft ohne Projekt-Umgebung:

    uv run --no-project --with "duckdb==2.0.0" \\
        python scripts/migrate_duckdb.py data/shotlake.duckdb [--replace]

Ohne `--replace` entsteht nur `shotlake.migrated.duckdb` neben der Datei. Mit `--replace` wird das
Original als `shotlake.duckdb.original` aufbewahrt (nie gelöscht) und die Kopie nimmt seinen Platz
ein. Vorher Shotlake beenden. Die Zeilenzahlen aller Tabellen werden verglichen.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

STORAGE = "v1.0.0"


def _sql(path: Path) -> str:
    return str(path).replace("'", "''")


def migrate(source: Path, target: Path) -> dict[str, int]:
    """Kopie im alten Format schreiben und Zeilenzahlen prüfen; liefert Zeilen je Tabelle."""
    if target.exists():
        raise FileExistsError(f"{target} existiert schon")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{_sql(source)}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{_sql(target)}' AS dst (STORAGE_VERSION '{STORAGE}')")
        con.execute("COPY FROM DATABASE src TO dst")
        tables = [
            r[0]
            for r in con.execute(
                "select table_name from duckdb_tables() where database_name = 'src'"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for table in tables:
            a = con.execute(f'select count(*) from src."{table}"').fetchone()[0]  # type: ignore[index]
            b = con.execute(f'select count(*) from dst."{table}"').fetchone()[0]  # type: ignore[index]
            if a != b:
                raise RuntimeError(f"Zeilenzahl von {table} stimmt nicht: {a} gegen {b}")
            counts[table] = int(a)
        con.execute("DETACH dst")
        con.execute("DETACH src")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("database", type=Path)
    ap.add_argument(
        "--replace", action="store_true", help="Kopie an die Stelle des Originals setzen"
    )
    args = ap.parse_args(argv)
    source: Path = args.database
    if not source.exists():
        print(f"Fehler: {source} nicht gefunden", file=sys.stderr)
        return 1
    target = source.with_name(source.stem + ".migrated.duckdb")
    try:
        counts = migrate(source, target)
    except (duckdb.Error, FileExistsError, RuntimeError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    print(f"Geschrieben: {target} (DuckDB {duckdb.__version__}, Format {STORAGE})")
    print(f"  {len(counts)} Tabellen, {sum(counts.values())} Zeilen, alle Zeilenzahlen stimmen")
    if not args.replace:
        print("Zum Einsetzen: noch einmal mit --replace aufrufen (Original bleibt erhalten).")
        return 0
    original = source.with_name(source.name + ".original")
    if original.exists():
        print(f"Fehler: {original} existiert schon, nichts ersetzt", file=sys.stderr)
        return 1
    source.rename(original)
    target.rename(source)
    source.with_name(source.name + ".wal").unlink(missing_ok=True)
    print(f"Eingesetzt. Das Original liegt unverändert als {original}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
