"""Idempotenter Import: raw ablegen -> parsen -> canonical upsert -> Report."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .connectors.errors import SchemaDriftError
from .connectors.registry import CONNECTORS, get_connector
from .db import SHOT_FIELDS, naive_utc
from .model import ParsedSession
from .quality import assess


@dataclass
class ImportReport:
    run_id: str
    source: str
    filename: str
    status: str = "ok"  # ok | duplicate_file | drift_detected | error
    sessions_new: int = 0
    shots_new: int = 0
    shots_changed: int = 0
    shots_unchanged: int = 0
    rounds_new: int = 0
    rounds_changed: int = 0
    course_shots_new: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    flag_counts: dict[str, int] = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [f"Import {self.source}: {self.filename} -> {self.status}"]
        if self.status == "ok":
            lines.append(
                f"  Sessions neu: {self.sessions_new}; Schläge neu {self.shots_new}, "
                f"geändert {self.shots_changed}, unverändert {self.shots_unchanged}"
            )
            if self.rounds_new or self.rounds_changed or self.course_shots_new:
                lines.append(
                    f"  Runden neu: {self.rounds_new}, aktualisiert {self.rounds_changed}; "
                    f"Platzschläge geschrieben: {self.course_shots_new}"
                )
        elif self.status == "duplicate_file":
            lines.append("  Datei bereits importiert (Duplikat), nichts geändert")
        for flag, n in sorted(self.flag_counts.items()):
            lines.append(f"  Hinweis {flag}: {n}")
        lines.extend(f"  Warnung: {w}" for w in self.warnings)
        lines.extend(f"  Fehler: {e}" for e in self.errors)
        return "\n".join(lines)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_run(con: duckdb.DuckDBPyConnection, rep: ImportReport, started: datetime) -> None:
    con.execute(
        "insert into import_run values (?,?,?,?,?,?,?,?,?,?,?)",
        [
            rep.run_id,
            rep.source,
            rep.filename,
            rep.status,
            started,
            _now(),
            rep.sessions_new,
            rep.shots_new,
            rep.shots_changed,
            rep.shots_unchanged,
            json.dumps(rep.errors) if rep.errors else None,
        ],
    )


def import_file(
    con: duckdb.DuckDBPyConnection, source: str, path: Path | str, raw_dir: Path | None
) -> ImportReport:
    path = Path(path)
    if source not in CONNECTORS:
        raise ValueError(f"Unbekannte Quelle: {source}")
    connector = get_connector(source)
    started = _now()
    rep = ImportReport(run_id=uuid.uuid4().hex, source=source, filename=path.name)
    digest = _sha256(path)

    known = con.execute(
        "select 1 from raw_object where source = ? and content_hash = ?", [source, digest]
    ).fetchone()
    if known:
        rep.status = "duplicate_file"
        _record_run(con, rep, started)
        return rep

    stored: str | None = None
    if raw_dir is not None:
        target = raw_dir / source / f"{digest[:12]}_{path.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        stored = str(target)
    con.execute(
        "insert into raw_object values (?,?,?,?,?,?)",
        [source, digest, path.name, path.stat().st_size, stored, started],
    )

    try:
        parsed = connector.parse(path)
    except SchemaDriftError as exc:
        rep.status = "drift_detected"
        rep.errors.append(str(exc))
        _record_run(con, rep, started)
        return rep

    rep.errors.extend(parsed.errors)
    rep.warnings.extend(parsed.warnings)
    _write(con, parsed, digest, rep)
    _record_run(con, rep, started)
    return rep


def _write(
    con: duckdb.DuckDBPyConnection, parsed: ParsedSession, digest: str, rep: ImportReport
) -> None:
    sid = parsed.session_id
    flags: Counter[str] = Counter()
    con.begin()
    try:
        if con.execute("select 1 from session where session_id = ?", [sid]).fetchone() is None:
            con.execute(
                "insert into session values (?,?,?,?,?,?,?)",
                [
                    sid,
                    parsed.source,
                    parsed.session_key,
                    parsed.kind,
                    naive_utc(parsed.start_utc),
                    digest,
                    rep.run_id,
                ],
            )
            rep.sessions_new += 1
        columns = [
            "shot_id",
            "session_id",
            "shot_time_utc",
            *SHOT_FIELDS,
            "flags",
            "content_hash",
            "raw_hash",
            "import_run_id",
        ]
        placeholders = ",".join("?" for _ in columns)
        for raw_shot in parsed.shots:
            shot = assess(raw_shot)
            flags.update(f for f in shot.flags if f in ("outlier", "inconsistent"))
            shot_id = f"{sid}|{shot.shot_key}"
            chash = shot.content_hash()
            existing = con.execute(
                "select content_hash from shot where shot_id = ?", [shot_id]
            ).fetchone()
            if existing and existing[0] == chash:
                rep.shots_unchanged += 1
                continue
            values: list[object] = [shot_id, sid, naive_utc(shot.shot_time_utc)]
            values += [getattr(shot, name) for name in SHOT_FIELDS]
            values += [list(shot.flags), chash, digest, rep.run_id]
            con.execute(
                f"insert or replace into shot ({','.join(columns)}) values ({placeholders})",
                values,
            )
            if existing:
                rep.shots_changed += 1
            else:
                rep.shots_new += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    rep.flag_counts = dict(flags)


def sync_location(
    con: duckdb.DuckDBPyConnection, source: str, location: Path | str, raw_dir: Path | None
) -> list[ImportReport]:
    """Alle Dateien einer Quelle importieren; bekannte (gleicher Hash) werden übersprungen."""
    connector = get_connector(source)
    return [import_file(con, source, f, raw_dir) for f in connector.discover(Path(location))]
