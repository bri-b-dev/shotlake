"""Gesamt-Export aller Tabellen als Parquet und CSV, mit Schema, Versionsangabe und Prüfsummen.

Ergebnis (Verzeichnis oder ZIP):
- `parquet/<tabelle>.parquet` (ZSTD) und `csv/<tabelle>.csv` (UTF-8, Komma, Kopfzeile),
- `schema.json`: Version, Konventionen (Zeit, Einheiten), je Tabelle Beschreibung, Spalten mit Typ
  und Einheit, Primärschlüssel, Zeilenzahl, Dateien mit SHA-256,
- `README.md`: Kurzanleitung zum Einlesen.
Der Export liest die Datenbank in einer Transaktion (einheitlicher Stand) und enthält weder
Zugangsdaten noch Schlüssel. Er enthält aber deine Rohwerte (zum Beispiel GPS-Positionen).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import duckdb

EXPORT_FORMAT = 1
FORMATS = ("parquet", "csv")

TABLES: dict[str, str] = {
    "round": "Platzrunden aller Quellen (eine Zeile je Quelle; Dubletten über round_match)",
    "hole": "Löcher einer Runde: Par, Lochindex, Schläge, Putts, Strafschläge, Fairway",
    "course_shot": "Einzelne Schläge auf dem Platz mit Lage, Distanz und Position",
    "course": "Plätze je Quelle",
    "course_tee": "Abschläge (Tees) eines Platzes mit Rating und Slope je Wertung",
    "tee_rating": "Von dir hinterlegte Rating/Slope-Werte für 9- oder 18-Loch-Teile",
    "session": "Range-, Bay- und Simulator-Sessions",
    "shot": "Radar-/Launch-Monitor-Schläge einer Session mit Messwerten",
    "round_input": "Deine Eingaben zu Runden: handicap-relevant, Index, PCC, AGS, Notiz",
    "note": "Deine Notizen (Equipment, Schwung, Trainer, Runde, allgemein)",
    "round_match": "Dubletten-Verknüpfungen zwischen Runden aus mehreren Quellen",
    "course_master": "Deine Platz-Stammdaten (Name, Ort, Koordinaten, Standard-Tee, Zweitnamen)",
    "course_master_tee": "Rating und Slope je Tee und Teil (18 Löcher, vordere/hintere Neun)",
    "course_master_link": "Ausdrückliche Zuordnung von Plätzen der Quellen zu Stammplätzen",
    "club_map": "Zuordnung quellenspezifischer Schlägernamen zu kanonischen Typen",
    "raw_object": "Verzeichnis der gespeicherten Rohdaten (Quelle, Hash, Datei, Größe, Zeitpunkt)",
    "import_run": "Importläufe mit Ergebnis und Fehlern (Herkunftsnachweis)",
    "sync_state": "Fortschritt (Cursor) der API-Quellen",
}
UNITS = {
    "_m": "Meter",
    "_ms": "Meter pro Sekunde",
    "_deg": "Grad",
    "_rpm": "Umdrehungen pro Minute",
    "_utc": "UTC",
    "_lat": "Grad (WGS84)",
    "_lon": "Grad (WGS84)",
}
CONVENTIONS = {
    "encoding": "UTF-8",
    "timestamps": "UTC; Parquet als TIMESTAMP ohne Zeitzone, CSV als `YYYY-MM-DD HH:MM:SS`",
    "csv": "Trennzeichen Komma, Kopfzeile, Anführungszeichen bei Bedarf, Listen als [a, b]",
    "null": "Parquet: NULL; CSV: leeres Feld",
    "units": UNITS,
    "identifiers": "round_id = Quelle|Rundenschlüssel; session_id = Quelle:Sessionschlüssel",
    "sources": "arccos, bebrassie, garmin_platform, garmin_range, mevo, trackman",
}


@dataclass
class ExportResult:
    directory: Path
    tables: dict[str, int]
    files: int


def _sql(path: Path) -> str:
    return str(path).replace("'", "''")


def _unit(column: str) -> str | None:
    for suffix, unit in UNITS.items():
        if column.endswith(suffix):
            return unit
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _version() -> str:
    try:
        return metadata.version("shotlake")
    except metadata.PackageNotFoundError:
        return "unbekannt"


def export_all(
    db: Path,
    out: Path,
    *,
    formats: tuple[str, ...] = FORMATS,
    now: datetime | None = None,
) -> ExportResult:
    """Alle Tabellen nach `out` schreiben (Verzeichnis neu oder leer)."""
    if not db.exists():
        raise FileNotFoundError("Keine Datenbank: nichts zu exportieren")
    unknown = set(formats) - set(FORMATS)
    if unknown or not formats:
        raise ValueError(f"Unbekanntes Format: {sorted(unknown) or 'keines gewählt'}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} ist nicht leer")
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=True)
    tables_meta: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    files = 0
    try:
        con.execute("BEGIN TRANSACTION")
        found = {r[0] for r in con.execute("select table_name from duckdb_tables()").fetchall()}
        ordered = [n for n in TABLES if n in found] + sorted(found - set(TABLES))
        for name in ordered:
            cols = con.execute(
                "select column_name, data_type, is_nullable from duckdb_columns() "
                "where table_name = ? order by column_index",
                [name],
            ).fetchall()
            pk = con.execute(
                "select constraint_column_names from duckdb_constraints() "
                "where table_name = ? and constraint_type = 'PRIMARY KEY'",
                [name],
            ).fetchall()
            rows = int(con.execute(f'select count(*) from "{name}"').fetchone()[0])  # type: ignore[index]
            written: dict[str, dict[str, Any]] = {}
            for fmt in formats:
                (out / fmt).mkdir(exist_ok=True)
                target = out / fmt / f"{name}.{'parquet' if fmt == 'parquet' else 'csv'}"
                options = (
                    "FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet" else "FORMAT CSV, HEADER"
                )
                con.execute(
                    f"COPY (select * from \"{name}\" order by all) TO '{_sql(target)}' ({options})"
                )
                written[fmt] = {
                    "path": target.relative_to(out).as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": _sha256(target),
                }
                files += 1
            counts[name] = rows
            tables_meta.append(
                {
                    "name": name,
                    "description": TABLES.get(name, ""),
                    "rows": rows,
                    "primary_key": list(pk[0][0]) if pk else [],
                    "columns": [
                        {
                            "name": c,
                            "type": t,
                            "nullable": bool(n),
                            **({"unit": u} if (u := _unit(c)) else {}),
                        }
                        for c, t, n in cols
                    ],
                    "files": written,
                }
            )
        con.execute("COMMIT")
    finally:
        con.close()
    created = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    schema = {
        "export_format": EXPORT_FORMAT,
        "created": created,
        "shotlake": _version(),
        "duckdb": duckdb.__version__,
        "conventions": CONVENTIONS,
        "tables": tables_meta,
    }
    (out / "schema.json").write_text(json.dumps(schema, indent=1, ensure_ascii=False) + "\n")
    (out / "README.md").write_text(_readme(created, counts))
    return ExportResult(out, counts, files + 2)


def export_zip(db: Path, target: Path, *, formats: tuple[str, ...] = FORMATS) -> ExportResult:
    """Export als ZIP (für den Download in der Oberfläche)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".export-") as tmp_name:
        result = export_all(db, Path(tmp_name) / "export", formats=formats)
        tmp_zip = Path(tmp_name) / "out.zip"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(result.directory.rglob("*")):
                if path.is_file():
                    archive.write(path, f"shotlake-export/{path.relative_to(result.directory)}")
        tmp_zip.replace(target)
    return ExportResult(target, result.tables, result.files)


def _readme(created: str, counts: dict[str, int]) -> str:
    listing = "\n".join(f"- `{n}`: {counts[n]} Zeilen. {TABLES.get(n, '')}" for n in counts)
    return f"""# Shotlake-Export

Erstellt: {created} (Format {EXPORT_FORMAT}). Tabellen und Spalten beschreibt `schema.json`.

{listing}

Einlesen:

    # DuckDB
    select * from read_parquet('parquet/round.parquet');
    # pandas
    import pandas as pd; pd.read_parquet("parquet/round.parquet")

Zeiten sind UTC. Einheiten stehen im Spaltennamen (`_m` Meter, `_ms` m/s, `_deg` Grad, `_rpm`).
Die Dateien enthalten deine Rohwerte inklusive Positionen, aber keine Zugangsdaten.
"""
