"""Inkrementeller Sync für API-Connectors: fetch(cursor) -> raw -> parse -> canonical."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .connectors.api import ApiConnector, AuthError, RawRecord, TransientError, log_retry
from .connectors.errors import SchemaDriftError
from .ingest import ImportReport, _record_run, _write, log_started
from .ingest_round import write_round
from .model import ParsedRound


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_cursor(con: duckdb.DuckDBPyConnection, source: str) -> str | None:
    row = con.execute("select cursor from sync_state where source = ?", [source]).fetchone()
    return None if row is None else row[0]


def _set_cursor(con: duckdb.DuckDBPyConnection, source: str, cursor: str | None) -> None:
    con.execute("insert or replace into sync_state values (?,?,?)", [source, cursor, _now()])


UNMAPPED_PREFIX = "Schläger ohne Zuordnung: "


def drop_mapped_club_warnings(
    con: duckdb.DuckDBPyConnection, source: str, warnings: list[str]
) -> list[str]:
    """Warnungen zu Schlägern entfernen, die inzwischen in `club_map` zugeordnet sind."""
    import ast

    mapped = {
        r[0]
        for r in con.execute("select club_ref from club_map where source = ?", [source]).fetchall()
    }
    out: list[str] = []
    for w in warnings:
        if not w.startswith(UNMAPPED_PREFIX):
            out.append(w)
            continue
        try:
            names = ast.literal_eval(w[len(UNMAPPED_PREFIX) :])
        except (ValueError, SyntaxError):
            out.append(w)
            continue
        rest = [n for n in names if n not in mapped]
        if rest:
            out.append(f"{UNMAPPED_PREFIX}{rest}")
    return out


def _process(
    con: duckdb.DuckDBPyConnection,
    connector: ApiConnector,
    record: RawRecord,
    raw_dir: Path | None,
    rep: ImportReport,
    started: datetime,
) -> None:
    data = record.to_bytes()
    stable = RawRecord(record.external_id, connector.stable_payload(record), record.kind)
    digest = hashlib.sha256(stable.to_bytes()).hexdigest()
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
    rep.warnings.extend(drop_mapped_club_warnings(con, connector.id, parsed.warnings))
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
    log_started(rep)
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
                delay = connector.retry.delay(attempt, exc)
                log_retry(attempt, connector.retry.max_attempts, delay, exc)
                connector.sleep(delay)
    except SchemaDriftError as exc:
        rep.status = "drift_detected"
        rep.errors.append(str(exc))
    except AuthError as exc:
        rep.status = "error"
        rep.errors.append(f"Auth: {exc}")
    except TransientError as exc:
        rep.status = "error"
        rep.errors.append(f"Quelle nicht erreichbar: {exc}")
    rep.warnings.extend(connector.notes)
    _record_run(con, rep, started)
    return rep


def reprocess_raw(con: duckdb.DuckDBPyConnection, connector: ApiConnector) -> ImportReport:
    """Canonical aus den gespeicherten Rohdokumenten neu aufbauen (kein Netzzugriff).

    Pro Dokument gilt die zuletzt importierte Fassung. Nutzereingaben (`handicap_relevant`)
    bleiben erhalten, unveränderte Zeilen werden per Inhalts-Hash übersprungen.
    """
    import json

    started = _now()
    rep = ImportReport(run_id=uuid.uuid4().hex, source=connector.id, filename="(reprocess)")
    log_started(rep)
    rows = con.execute(
        "select filename, content_hash, stored_path from raw_object "
        "where source = ? and stored_path is not null order by imported_at",
        [connector.id],
    ).fetchall()
    latest: dict[str, tuple[str, str]] = {}
    for external_id, digest, path in rows:
        latest[external_id] = (digest, path)  # später importiert überschreibt früher
    try:
        for external_id, (digest, path) in latest.items():
            payload = json.loads(Path(path).read_text())
            parsed = connector.parse_record(RawRecord(external_id, payload))
            rep.errors.extend(parsed.errors)
            rep.warnings.extend(drop_mapped_club_warnings(con, connector.id, parsed.warnings))
            if isinstance(parsed, ParsedRound):
                write_round(con, parsed, digest, rep)
            else:
                _write(con, parsed, digest, rep)
    except SchemaDriftError as exc:
        rep.status = "drift_detected"
        rep.errors.append(str(exc))
    _record_run(con, rep, started)
    return rep
