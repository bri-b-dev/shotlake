"""Gesamt-Export: Vollständigkeit, Schema, Prüfsummen, Formate, Download."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from shotlake.cli import main
from shotlake.connectors.trackman import TrackmanConnector
from shotlake.db import SCHEMA, open_db
from shotlake.export import EXPORT_FORMAT, TABLES, export_all, export_zip
from shotlake.ingest_api import sync_api
from shotlake.web.app import create_app
from shotlake.workspace import Workspace
from tests.test_api_fixtures import TrackmanFakeClient
from tests.test_backup import make_data

REPO = Path(__file__).parent.parent
CLUB_MAP = REPO / "config" / "club_map.toml"
BASELINE = REPO / "baselines" / "hcp18w-approx-v1.toml"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def filled(tmp_path: Path) -> Path:
    """Datenverzeichnis mit echten Parser-Daten (Trackman-Fixtures) und Nutzereingaben."""
    data = tmp_path / "data"
    con = open_db(data / "shotlake.duckdb")
    sync_api(con, TrackmanConnector(client=TrackmanFakeClient()), data / "raw")
    con.execute(
        "insert into round_input (round_id, handicap_relevant, note, updated_at) "
        "values ('x', true, 'Notiz mit Komma, \"Anführungszeichen\" und Ümlaut', now()::timestamp)"
    )
    con.close()
    return data / "shotlake.duckdb"


def test_every_table_is_described_and_exported(tmp_path: Path, filled: Path) -> None:
    result = export_all(filled, tmp_path / "out", now=NOW)
    con = duckdb.connect(str(filled), read_only=True)
    db_tables = {r[0] for r in con.execute("select table_name from duckdb_tables()").fetchall()}
    con.close()
    assert set(result.tables) == db_tables
    assert db_tables <= set(TABLES), "neue Tabelle ohne Beschreibung in export.TABLES"
    schema = json.loads((tmp_path / "out" / "schema.json").read_text())
    assert schema["export_format"] == EXPORT_FORMAT and schema["created"].startswith("2026-11-19")
    assert {t["name"] for t in schema["tables"]} == db_tables
    assert all(t["description"] for t in schema["tables"])


def test_row_counts_match_in_database_parquet_csv_and_schema(tmp_path: Path, filled: Path) -> None:
    export_all(filled, tmp_path / "out")
    schema = json.loads((tmp_path / "out" / "schema.json").read_text())
    db = duckdb.connect(str(filled), read_only=True)
    mem = duckdb.connect(":memory:")
    for table in schema["tables"]:
        name = table["name"]
        expected = db.execute(f'select count(*) from "{name}"').fetchone()[0]  # type: ignore[index]
        parquet = tmp_path / "out" / table["files"]["parquet"]["path"]
        got = mem.execute(f"select count(*) from read_parquet('{parquet}')").fetchone()[0]  # type: ignore[index]
        rows = list(
            csv.reader(io.StringIO((tmp_path / "out" / table["files"]["csv"]["path"]).read_text()))
        )
        assert expected == got == table["rows"] == len(rows) - 1, name
        assert rows[0] == [c["name"] for c in table["columns"]], name


def test_checksums_and_units_in_schema(tmp_path: Path, filled: Path) -> None:
    export_all(filled, tmp_path / "out")
    schema = json.loads((tmp_path / "out" / "schema.json").read_text())
    for table in schema["tables"]:
        for entry in table["files"].values():
            blob = (tmp_path / "out" / entry["path"]).read_bytes()
            assert (
                hashlib.sha256(blob).hexdigest() == entry["sha256"] and len(blob) == entry["bytes"]
            )
    shot = next(t for t in schema["tables"] if t["name"] == "shot")
    units = {c["name"]: c.get("unit") for c in shot["columns"]}
    assert units["carry_m"] == "Meter" and units["ball_speed_ms"] == "Meter pro Sekunde"
    assert units["shot_time_utc"] == "UTC" and shot["primary_key"] == ["shot_id"]


def test_csv_quoting_and_umlauts_roundtrip(tmp_path: Path, filled: Path) -> None:
    export_all(filled, tmp_path / "out")
    text = (tmp_path / "out" / "csv" / "round_input.csv").read_text(encoding="utf-8")
    row = next(iter(csv.DictReader(io.StringIO(text))))
    assert row["note"] == 'Notiz mit Komma, "Anführungszeichen" und Ümlaut'
    mem = duckdb.connect(":memory:")
    note = mem.execute(
        f"select note from read_parquet('{tmp_path / 'out' / 'parquet' / 'round_input.parquet'}')"
    ).fetchone()
    assert note == ('Notiz mit Komma, "Anführungszeichen" und Ümlaut',)


def test_export_is_deterministic(tmp_path: Path, filled: Path) -> None:
    export_all(filled, tmp_path / "a", now=NOW)
    export_all(filled, tmp_path / "b", now=NOW)
    for path in sorted((tmp_path / "a").rglob("*")):
        if path.is_file():
            assert (
                path.read_bytes()
                == (tmp_path / "b" / path.relative_to(tmp_path / "a")).read_bytes()
            )


def test_single_format_and_guards(tmp_path: Path, filled: Path) -> None:
    export_all(filled, tmp_path / "csv_only", formats=("csv",))
    assert not (tmp_path / "csv_only" / "parquet").exists()
    assert (tmp_path / "csv_only" / "csv" / "round.csv").exists()
    with pytest.raises(FileExistsError):
        export_all(filled, tmp_path / "csv_only")  # nie in ein belegtes Verzeichnis schreiben
    with pytest.raises(ValueError, match="Format"):
        export_all(filled, tmp_path / "x", formats=("xlsx",))
    with pytest.raises(FileNotFoundError):
        export_all(tmp_path / "gibt-es-nicht.duckdb", tmp_path / "y")


def test_export_contains_no_credentials_or_key_files(tmp_path: Path) -> None:
    data = make_data(tmp_path / "data")  # mit secrets.enc, secret.key, auth.json, Token
    export_all(data / "shotlake.duckdb", tmp_path / "out")
    blob = b"".join(p.read_bytes() for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert b"TOKEN-GEHEIM" not in blob and b"AUTH-GEHEIM" not in blob
    names = " ".join(p.name for p in (tmp_path / "out").rglob("*"))
    assert "secret" not in names and "auth" not in names


def test_schema_lists_every_table_of_the_database_schema() -> None:
    created = set()
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    created = {r[0] for r in con.execute("select table_name from duckdb_tables()").fetchall()}
    assert created == set(TABLES)


def test_zip_download_in_cli_and_web(tmp_path: Path, filled: Path, capsys) -> None:
    out = export_zip(filled, tmp_path / "e.zip")
    with zipfile.ZipFile(tmp_path / "e.zip") as archive:
        names = archive.namelist()
    assert (
        "shotlake-export/schema.json" in names and "shotlake-export/parquet/round.parquet" in names
    )
    assert out.files == len(names)
    data = filled.parent
    assert main(["--data-dir", str(data), "export", "--zip", "--out", str(tmp_path / "c.zip")]) == 0
    assert "Export:" in capsys.readouterr().out
    client = TestClient(create_app(data, CLUB_MAP, BASELINE), follow_redirects=False)
    r = client.post("/settings/export/download")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert (
        r.headers["content-type"] == "application/zip" and r.headers["cache-control"] == "no-store"
    )
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert "shotlake-export/csv/round.csv" in archive.namelist()
    assert not list(data.glob(".export-*"))
    assert "Export herunterladen" in client.get("/settings").text


def test_cli_format_and_existing_target(tmp_path: Path, filled: Path, capsys) -> None:
    data = filled.parent
    target = tmp_path / "csv"
    assert main(["--data-dir", str(data), "export", "--format", "csv", "--out", str(target)]) == 0
    assert (target / "csv" / "hole.csv").exists() and not (target / "parquet").exists()
    assert main(["--data-dir", str(data), "export", "--out", str(target)]) == 1
    assert "nicht leer" in capsys.readouterr().err


def test_export_is_refused_while_a_sync_runs(tmp_path: Path, filled: Path) -> None:
    from shotlake.backup import BackupError

    ws = Workspace(filled.parent, CLUB_MAP, BASELINE)
    assert ws._sync_lock.acquire(blocking=False)
    try:
        with pytest.raises(BackupError, match="Sync"):
            ws.export(tmp_path / "x.zip")
    finally:
        ws._sync_lock.release()
