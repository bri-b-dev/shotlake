"""Contract-Tests: jeder registrierte Connector muss dieselben Zusagen einhalten."""

from datetime import UTC
from pathlib import Path

import pytest

from shotlake.connectors.base import Connector
from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.registry import CONNECTORS, get_connector
from shotlake.db import open_db
from shotlake.ingest import import_file, sync_location

IDS = sorted(CONNECTORS)
RISKS = {"official", "unofficial_tos_risk", "file_only"}
MAX_CARRY_M = 450.0


def fixture_files(fixtures: Path, connector: Connector) -> list[Path]:
    files = connector.discover(fixtures / connector.id)
    assert files, f"keine Fixtures für {connector.id}"
    return files


def count(con, table: str) -> int:
    row = con.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return int(row[0])


def mutate_header(src: Path, dst: Path, old: str, new: str) -> Path:
    text = src.read_text(encoding="utf-8-sig")
    first, rest = text.split("\n", 1)
    dst.write_text(first.replace(old, new, 1) + "\n" + rest, encoding="utf-8-sig")
    return dst


def test_registry_contains_phase1_sources():
    assert {"mevo", "garmin_range"} <= set(CONNECTORS)
    assert get_connector("mevo").id == "mevo"
    with pytest.raises(KeyError):
        get_connector("unknown")


@pytest.mark.parametrize("cid", IDS)
def test_metadata(cid):
    c = CONNECTORS[cid]
    assert c.id == cid
    assert c.mode in {"file", "api"}
    assert c.risk in RISKS
    assert c.required_columns and c.required_columns <= c.known_columns


@pytest.mark.parametrize("cid", IDS)
def test_fixtures_satisfy_invariants(cid, fixtures):
    c = CONNECTORS[cid]
    for path in fixture_files(fixtures, c):
        session = c.parse(path)
        assert session.source == cid
        assert session.shots, path.name
        assert session.start_utc.tzinfo is UTC
        keys = [s.shot_key for s in session.shots]
        assert len(keys) == len(set(keys)), f"shot_key nicht eindeutig in {path.name}"
        for s in session.shots:
            assert s.source == cid
            assert s.measurement_method in {"radar", "camera", "sensor", "sim", "gps"}
            if s.carry_m is not None:
                assert 0 <= s.carry_m <= MAX_CARRY_M
            if s.ball_speed_ms is not None:
                assert 0 < s.ball_speed_ms < 110
            if s.club_type is None:
                assert "club_unmapped" in s.flags


@pytest.mark.parametrize("cid", IDS)
def test_contract_check_passes_on_fixtures(cid, fixtures):
    c = CONNECTORS[cid]
    for path in fixture_files(fixtures, c):
        report = c.contract_check(path)
        assert report.ok, (path.name, report.errors)
        assert report.warnings == (), (path.name, report.warnings)


@pytest.mark.parametrize("cid", IDS)
def test_unknown_column_is_warning_not_error(cid, fixtures, tmp_path):
    c = CONNECTORS[cid]
    src = fixture_files(fixtures, c)[0]
    dst = tmp_path / src.name
    text = src.read_text(encoding="utf-8-sig")
    first, rest = text.split("\n", 1)
    dst.write_text(first.rstrip("\r") + ",Brand New (x)\n" + rest, encoding="utf-8-sig")
    report = c.contract_check(dst)
    assert report.ok
    assert any("Brand New (x)" in w for w in report.warnings)
    session = c.parse(dst)
    assert any("Brand New (x)" in w for w in session.warnings)


@pytest.mark.parametrize("cid", IDS)
def test_missing_required_column_is_drift(cid, fixtures, tmp_path):
    c = CONNECTORS[cid]
    src = fixture_files(fixtures, c)[0]
    victim = sorted(c.required_columns)[0]
    dst = mutate_header(src, tmp_path / src.name, victim, "Renamed Column")
    report = c.contract_check(dst)
    assert not report.ok
    assert any(victim in e for e in report.errors)
    with pytest.raises(SchemaDriftError):
        c.parse(dst)


@pytest.mark.parametrize("cid", IDS)
def test_drift_import_keeps_raw_and_writes_nothing(cid, fixtures, tmp_path):
    c = CONNECTORS[cid]
    src = fixture_files(fixtures, c)[0]
    victim = sorted(c.required_columns)[0]
    dst = mutate_header(src, tmp_path / src.name, victim, "Renamed Column")
    con = open_db(":memory:")
    rep = import_file(con, cid, dst, raw_dir=tmp_path / "raw")
    assert rep.status == "drift_detected"
    assert count(con, "shot") == 0
    assert count(con, "raw_object") == 1


@pytest.mark.parametrize("cid", IDS)
def test_discover_returns_sorted_matching_files(cid, fixtures, tmp_path):
    c = CONNECTORS[cid]
    (tmp_path / "ignored.txt").write_text("x")
    for src in fixture_files(fixtures, c):
        (tmp_path / src.name).write_bytes(src.read_bytes())
    found = c.discover(tmp_path)
    assert found == sorted(found)
    assert all(p.suffix == ".csv" for p in found)


@pytest.mark.parametrize("cid", IDS)
def test_sync_is_incremental_via_content_hash(cid, fixtures, tmp_path):
    c = CONNECTORS[cid]
    con = open_db(":memory:")
    first = sync_location(con, cid, fixtures / cid, raw_dir=tmp_path / "raw")
    assert all(r.status == "ok" for r in first)
    second = sync_location(con, cid, fixtures / cid, raw_dir=tmp_path / "raw")
    assert [r.status for r in second] == ["duplicate_file"] * len(first)
    assert len(first) == len(c.discover(fixtures / cid))


def test_unknown_columns_show_up_in_import_report(fixtures, tmp_path):
    src = fixtures / "mevo" / "08-may-2026_05_17_pm.csv"
    text = src.read_text(encoding="utf-8")
    first, rest = text.split("\n", 1)
    dst = tmp_path / src.name
    dst.write_text(first + ",Brand New (x)\n" + rest, encoding="utf-8")
    con = open_db(":memory:")
    rep = import_file(con, "mevo", dst, raw_dir=None)
    assert rep.status == "ok"
    assert any("Brand New (x)" in w for w in rep.warnings)
    assert "Brand New (x)" in rep.as_text()
