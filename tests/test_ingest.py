import pytest

from shotlake.db import open_db
from shotlake.ingest import import_file

MEVO = "mevo/08-may-2026_05_17_pm.csv"
GARMIN = "garmin_range/DrivingRange-2024-04-06 13-52-17-444.csv"


@pytest.fixture
def con():
    c = open_db(":memory:")
    yield c
    c.close()


def count(con, table):
    return con.execute(f"select count(*) from {table}").fetchone()[0]


def test_first_import_reports_new_records(con, fixtures, tmp_path):
    r = import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    assert r.status == "ok"
    assert (r.sessions_new, r.shots_new, r.shots_changed, r.shots_unchanged) == (1, 2, 0, 0)
    assert count(con, "shot") == 2 and count(con, "session") == 1


def test_reimport_of_same_file_is_idempotent(con, fixtures, tmp_path):
    import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    r = import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    assert r.status == "duplicate_file"
    assert (r.shots_new, r.shots_changed) == (0, 0)
    assert count(con, "shot") == 2 and count(con, "raw_object") == 1


def test_changed_export_updates_existing_shot(con, fixtures, tmp_path):
    import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    edited = tmp_path / "08-may-2026_05_17_pm.csv"
    text = (fixtures / MEVO).read_text(encoding="utf-8")
    edited.write_text(text.replace("106.8", "110.0"), encoding="utf-8")
    r = import_file(con, "mevo", edited, raw_dir=tmp_path / "raw")
    assert (r.shots_new, r.shots_changed, r.shots_unchanged) == (0, 1, 1)
    carry = con.execute("select carry_m from shot where shot_key = '5 Wood|1'").fetchone()[0]
    assert carry == pytest.approx(110.0 * 0.9144)
    assert count(con, "shot") == 2
    assert count(con, "raw_object") == 2  # beide Fassungen bleiben erhalten


def test_raw_file_is_stored_unchanged(con, fixtures, tmp_path):
    raw_dir = tmp_path / "raw"
    import_file(con, "mevo", fixtures / MEVO, raw_dir=raw_dir)
    stored = list((raw_dir / "mevo").iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes() == (fixtures / MEVO).read_bytes()


def test_provenance_links_shot_to_raw_and_run(con, fixtures, tmp_path):
    r = import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    row = con.execute(
        "select s.raw_hash, s.import_run_id, o.content_hash from shot s "
        "join raw_object o on o.content_hash = s.raw_hash limit 1"
    ).fetchone()
    assert row[0] == row[2] and row[1] == r.run_id


def test_sources_are_kept_separate(con, fixtures, tmp_path):
    import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    import_file(con, "garmin_range", fixtures / GARMIN, raw_dir=tmp_path / "raw")
    sources = {x[0] for x in con.execute("select distinct source from shot").fetchall()}
    assert sources == {"mevo", "garmin_range"}
    assert count(con, "session") == 2


def test_schema_drift_keeps_raw_but_writes_no_data(con, tmp_path):
    bad = tmp_path / "01-jan-2026_10_00_am.csv"
    bad.write_text("club,Shot,Ball (km/h),Carry (m),Total (m)\nDriver,1,160,200,210\n")
    r = import_file(con, "mevo", bad, raw_dir=tmp_path / "raw")
    assert r.status == "drift_detected"
    assert count(con, "shot") == 0
    assert count(con, "raw_object") == 1
    assert r.errors


def test_implausible_values_are_flagged_not_dropped(con, tmp_path):
    p = tmp_path / "01-jan-2026_10_00_am.csv"
    p.write_text(
        "club,Shot,Ball (mph),Carry (yds),Total (yds)\n"
        "Driver,1,100,700,710\n"
        "Driver,2,100,200,150\n"
        "Driver,3,100,200,210\n",
        encoding="utf-8",
    )
    r = import_file(con, "mevo", p, raw_dir=tmp_path / "raw")
    flags = dict(con.execute("select shot_key, flags from shot order by shot_key").fetchall())
    assert "outlier" in flags["Driver|1"]
    assert "inconsistent" in flags["Driver|2"]
    assert flags["Driver|3"] == []
    assert r.flag_counts == {"outlier": 1, "inconsistent": 1}


def test_report_text_mentions_counts(con, fixtures, tmp_path):
    r = import_file(con, "mevo", fixtures / MEVO, raw_dir=tmp_path / "raw")
    text = r.as_text()
    assert "neu" in text and "2" in text


def test_without_raw_dir_nothing_is_copied(con, fixtures):
    r = import_file(con, "mevo", fixtures / MEVO, raw_dir=None)
    assert r.status == "ok"
