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
