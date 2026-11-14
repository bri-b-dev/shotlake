"""Inkrementeller Sync für API-Connectors: fetch(cursor) -> raw -> parse -> canonical."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .connectors.api import ApiConnector, AuthError, RawRecord, TransientError
from .connectors.errors import SchemaDriftError
from .ingest import ImportReport, _record_run, _write
from .ingest_round import write_round
from .model import ParsedRound


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_cursor(con: duckdb.DuckDBPyConnection, source: str) -> str | None:
    row = con.execute("select cursor from sync_state where source = ?", [source]).fetchone()
    return None if row is None else row[0]


def _set_cursor(con: duckdb.DuckDBPyConnection, source: str, cursor: str | None) -> None:
    con.execute("insert or replace into sync_state values (?,?,?)", [source, cursor, _now()])


def _process(
    con: duckdb.DuckDBPyConnection,
    connector: ApiConnector,
    record: RawRecord,
    raw_dir: Path | None,
    rep: ImportReport,
    started: datetime,
) -> None:
    data = record.to_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if con.execute(
        "select 1 from raw_object where source = ? and content_hash = ?", [connector.id, digest]
    ).fetchone():
        return
    stored: str | None = None
    if raw_dir is not None:
        target = raw_dir / connector.id / f"{digest[:12]}_{record.external_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        stored = str(target)
    report = connector.contract_check(record)
    rep.warnings.extend(report.warnings)
    if not report.ok:
        raise SchemaDriftError("; ".join(report.errors))
    parsed = connector.parse_record(record)
    rep.errors.extend(parsed.errors)
    rep.warnings.extend(parsed.warnings)
    # Hash erst nach erfolgreichem Parsen registrieren: ein Drift-Dokument wird nach
    # einem Connector-Fix erneut verarbeitet.
    con.execute(
        "insert into raw_object values (?,?,?,?,?,?)",
        [connector.id, digest, record.external_id, len(data), stored, started],
    )
    if isinstance(parsed, ParsedRound):
        write_round(con, parsed, digest, rep)
    else:
        _write(con, parsed, digest, rep)


def sync_api(
    con: duckdb.DuckDBPyConnection, connector: ApiConnector, raw_dir: Path | None
) -> ImportReport:
    """Seiten ab gespeichertem Cursor holen. Der Cursor rückt erst nach verarbeiteter Seite vor.

    Transiente Fehler lösen einen Neustart ab dem letzten Cursor mit Backoff aus; Fortschritt
    setzt den Zähler zurück. Drift oder Auth-Fehler brechen ab, der Cursor bleibt stehen.
    """
    started = _now()
    rep = ImportReport(run_id=uuid.uuid4().hex, source=connector.id, filename="(api)")
    attempt = 0
    try:
        connector.authenticate()
        cursor = get_cursor(con, connector.id)
        while True:
            try:
                for page in connector.fetch(cursor):
                    for record in page.records:
                        _process(con, connector, record, raw_dir, rep, started)
                    if page.next_cursor is not None:
                        cursor = page.next_cursor
                        _set_cursor(con, connector.id, cursor)
                    attempt = 0
                break
            except TransientError as exc:
                attempt += 1
                if attempt >= connector.retry.max_attempts:
                    raise
                connector.sleep(connector.retry.delay(attempt, exc))
    except SchemaDriftError as exc:
        rep.status = "drift_detected"
        rep.errors.append(str(exc))
    except AuthError as exc:
        rep.status = "error"
        rep.errors.append(f"Auth: {exc}")
    except TransientError as exc:
        rep.status = "error"
        rep.errors.append(f"Quelle nicht erreichbar: {exc}")
    _record_run(con, rep, started)
    return rep
