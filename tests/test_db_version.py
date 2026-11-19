"""DuckDB-Version: verständliche Fehlermeldung und Migration einer neueren Datenbankdatei."""

import importlib.util
import struct
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

from shotlake.cli import main
from shotlake.db import DatabaseVersionError, open_db

SCRIPT = Path(__file__).parent.parent / "scripts" / "migrate_duckdb.py"


def migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_duckdb", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(path: Path) -> None:
    con = open_db(path)
    con.execute(
        "insert into round_input (round_id, handicap_relevant, note, updated_at) "
        "values ('t|1', true, 'Eingabe', now()::timestamp), ('t|2', false, null, now()::timestamp)"
    )
    con.close()


NEWER = (
    "IO Error: Trying to read a database file with version number 999, but we can only read "
    "versions between 64 and 68.\nThe database file was created with a newer version of DuckDB."
)  # Wortlaut, den DuckDB 1.x beim Öffnen einer mit 2.x geschriebenen Datei meldet


def pretend_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Das Öffnen schlägt wie bei einer mit DuckDB 2.x geschriebenen Datei fehl."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise duckdb.IOException(NEWER)

    monkeypatch.setattr("shotlake.db.duckdb.connect", refuse)


def test_pyproject_keeps_duckdb_below_two() -> None:
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert '"duckdb>=1.4,<2"' in text and "AVX2" in text
    assert int(duckdb.__version__.split(".")[0]) == 1  # die geprüfte Umgebung ist 1.x


def test_newer_database_gives_an_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "shotlake.duckdb"
    pretend_newer(monkeypatch)
    with pytest.raises(DatabaseVersionError) as err:
        open_db(db)
    message = str(err.value)
    assert "neueren DuckDB-Version" in message and "AVX2" in message
    assert "scripts/migrate_duckdb.py" in message and str(db) in message


def test_cli_reports_newer_database_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pretend_newer(monkeypatch)
    assert main(["--data-dir", str(tmp_path), "rounds"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Fehler:") and "migrate_duckdb.py" in err and "Traceback" not in err


def test_migrate_copies_all_rows_and_keeps_the_original(tmp_path: Path) -> None:
    module = migrate_module()
    db = tmp_path / "shotlake.duckdb"
    make_db(db)
    assert module.main([str(db)]) == 0  # ohne --replace nur eine Kopie
    migrated = tmp_path / "shotlake.migrated.duckdb"
    assert migrated.exists()
    header = migrated.read_bytes()[:20]
    assert struct.unpack("<Q", header[12:20])[0] == 64  # Format von DuckDB 1.0
    assert module.main([str(db), "--replace"]) == 1  # Kopie liegt schon da: nichts überschreiben
    migrated.unlink()
    assert module.main([str(db), "--replace"]) == 0
    assert (tmp_path / "shotlake.duckdb.original").exists()  # Original nie gelöscht
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from round_input").fetchone() == (2,)
    con.close()


def test_migrate_refuses_to_replace_twice_and_missing_files(tmp_path: Path) -> None:
    module = migrate_module()
    db = tmp_path / "shotlake.duckdb"
    make_db(db)
    assert module.main([str(db), "--replace"]) == 0
    assert module.main([str(db), "--replace"]) == 1  # .original existiert: nichts ersetzt
    assert module.main([str(tmp_path / "gibt-es-nicht.duckdb")]) == 1


def test_other_io_errors_are_not_mistaken_for_a_version_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise duckdb.IOException("IO Error: Cannot open file: Permission denied")

    monkeypatch.setattr("shotlake.db.duckdb.connect", refuse)
    with pytest.raises(duckdb.IOException, match="Permission denied"):
        open_db(tmp_path / "x.duckdb")
