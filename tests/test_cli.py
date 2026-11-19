from shotlake.cli import main

MEVO_DIR = "mevo"


def run(args, data_dir):
    return main(["--data-dir", str(data_dir), *args])


def test_import_directory_then_stats(fixtures, tmp_path, capsys):
    assert run(["import", "mevo", str(fixtures / MEVO_DIR)], tmp_path) == 0
    out = capsys.readouterr().out
    assert "neu" in out
    assert run(["clubs", "--min-n", "1"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "wood_5" in out and "driver" in out


def test_second_import_reports_duplicates(fixtures, tmp_path, capsys):
    run(["import", "mevo", str(fixtures / MEVO_DIR)], tmp_path)
    capsys.readouterr()
    run(["import", "mevo", str(fixtures / MEVO_DIR)], tmp_path)
    out = capsys.readouterr().out
    assert "Duplikat" in out


def test_yard_display(fixtures, tmp_path, capsys):
    run(["import", "mevo", str(fixtures / MEVO_DIR / "08-may-2026_05_17_pm.csv")], tmp_path)
    capsys.readouterr()
    run(["clubs", "--min-n", "1", "--units", "yd"], tmp_path)
    out = capsys.readouterr().out
    assert "yd" in out


def test_drift_returns_nonzero(tmp_path, capsys):
    bad = tmp_path / "01-jan-2026_10_00_am.csv"
    bad.write_text("club,Shot,Ball (km/h),Carry (m),Total (m)\nDriver,1,160,200,210\n")
    assert run(["import", "mevo", str(bad)], tmp_path / "d") == 2


def test_outdoor_and_indoor_filters(fixtures, tmp_path, capsys):
    run(["import", "mevo", str(fixtures / MEVO_DIR / "08-may-2026_05_17_pm.csv")], tmp_path)
    capsys.readouterr()
    run(["clubs", "--min-n", "1", "--outdoor"], tmp_path)
    assert "wood_5" in capsys.readouterr().out
    run(["clubs", "--min-n", "1", "--indoor"], tmp_path)
    assert "wood_5" not in capsys.readouterr().out


def test_date_filters(fixtures, tmp_path, capsys):
    run(["import", "mevo", str(fixtures / MEVO_DIR / "08-may-2026_05_17_pm.csv")], tmp_path)
    capsys.readouterr()
    run(["clubs", "--min-n", "1", "--since", "2026-06-01"], tmp_path)
    assert "wood_5" not in capsys.readouterr().out
    run(["clubs", "--min-n", "1", "--since", "2026-05-01", "--until", "2026-05-08"], tmp_path)
    assert "wood_5" in capsys.readouterr().out


def test_sources_command_lists_risk(tmp_path, capsys):
    assert run(["sources"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "mevo" in out and "garmin_range" in out and "file_only" in out


def test_backup_verify_and_restore_commands(tmp_path, capsys, monkeypatch):
    from tests.test_backup import make_data, note_of

    data = make_data(tmp_path / "data", "cli")
    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)
    archive = tmp_path / "out.tar.gz"
    assert main(["--data-dir", str(data), "backup", "--out", str(archive)]) == 0
    assert "Nicht enthalten: Schlüssel" in capsys.readouterr().out
    assert main(["backup-verify", str(archive)]) == 0
    assert "In Ordnung" in capsys.readouterr().out
    target = tmp_path / "neu"
    assert main(["--data-dir", str(target), "restore", str(archive), "--yes"]) == 0
    assert note_of(target) == "cli"
    # ohne --yes und ohne Zustimmung ändert sich nichts
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert main(["--data-dir", str(data), "restore", str(archive)]) == 1
    assert note_of(data) == "cli" and not (data / "backups").exists()


def test_backup_password_comes_from_environment_never_from_arguments(tmp_path, capsys, monkeypatch):
    from tests.test_backup import make_data

    data = make_data(tmp_path / "data")
    monkeypatch.setenv("SHOTLAKE_BACKUP_PASSWORD", "env-passwort-1")
    archive = tmp_path / "e.enc"
    assert main(["--data-dir", str(data), "backup", "--password", "--out", str(archive)]) == 0
    assert archive.read_bytes().startswith(b"SHOTLAKE-BACKUP-ENC1")
    assert main(["backup-verify", str(archive)]) == 0
    monkeypatch.setenv("SHOTLAKE_BACKUP_PASSWORD", "falsch")
    assert main(["backup-verify", str(archive)]) == 1
    assert "Passwort falsch" in capsys.readouterr().err


def test_backup_key_command(tmp_path, capsys, monkeypatch):
    from tests.test_backup import make_data

    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)
    data = make_data(tmp_path / "data")
    assert main(["--data-dir", str(data), "backup-key"]) == 0
    assert capsys.readouterr().out.strip() == (data / "secret.key").read_text().strip()
    out = tmp_path / "k.key"
    assert main(["--data-dir", str(data), "backup-key", "--out", str(out)]) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert (
        main(["--data-dir", str(data), "backup-key", "--out", str(out)]) == 1
    )  # nie überschreiben
