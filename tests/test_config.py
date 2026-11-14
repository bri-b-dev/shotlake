"""Schlägerzuordnung als Konfigurationsdatei."""

from pathlib import Path

import pytest

from shotlake.cli import main
from shotlake.config import load_club_map, save_club_map, sync_club_map
from shotlake.db import open_db

REPO_CONFIG = Path(__file__).parent.parent / "config" / "club_map.toml"


def test_roundtrip_is_sorted_and_stable(tmp_path: Path):
    path = tmp_path / "c.toml"
    mapping = {"garmin_platform": {"ref_10": "iron_7", "ref_2": "driver"}, "a": {"x": "putter"}}
    save_club_map(path, mapping)
    assert load_club_map(path) == mapping
    first = path.read_text()
    save_club_map(path, load_club_map(path))
    assert path.read_text() == first
    assert first.index("[a]") < first.index("[garmin_platform]")
    assert first.index("ref_2") < first.index("ref_10")  # numerisch plausibel sortiert


def test_unknown_club_type_is_an_error(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('[s]\nx = "eisen_sieben"\n')
    with pytest.raises(ValueError, match="eisen_sieben"):
        load_club_map(path)


def test_sync_makes_file_authoritative(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    con.execute("insert into club_map values ('s', 'alt', 'putter')")
    path = tmp_path / "c.toml"
    assert sync_club_map(con, path) == 0  # keine Datei: Tabelle bleibt
    assert con.execute("select count(*) from club_map").fetchone() == (1,)
    save_club_map(path, {"s": {"neu": "driver"}})
    assert sync_club_map(con, path) == 1
    assert con.execute("select club_ref from club_map").fetchall() == [("neu",)]


def test_cli_club_set_writes_file_and_applies(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cfg = tmp_path / "cfg" / "club_map.toml"
    args = ["--data-dir", str(tmp_path / "d"), "--club-map", str(cfg)]
    assert main([*args, "club-set", "garmin_platform", "garmin_clubtype_1", "driver"]) == 0
    assert main([*args, "club-set", "garmin_platform", "garmin_clubtype_3", "wood_5"]) == 0
    assert load_club_map(cfg) == {
        "garmin_platform": {"garmin_clubtype_1": "driver", "garmin_clubtype_3": "wood_5"}
    }
    con = open_db(tmp_path / "d" / "shotlake.duckdb")
    assert con.execute("select count(*) from club_map").fetchone() == (2,)
    capsys.readouterr()


def test_repo_config_is_valid_and_complete():
    mapping = load_club_map(REPO_CONFIG)  # lehnt unbekannte Schlägertypen ab
    assert set(mapping) == {"garmin_platform", "arccos"}
    # Arccos: 12 aktive Schläger plus zwei ausgemusterte (Typ 42 = altes SW, Typ 37 = altes 7i)
    assert len(mapping["garmin_platform"]) == 11 and len(mapping["arccos"]) == 14
    assert mapping["garmin_platform"]["garmin_clubtype_23"] == "putter"
    assert mapping["arccos"]["arccos_clubtype_12"] == "putter"
    for source in mapping.values():  # je Quelle genau ein Putter und ein Driver
        assert list(source.values()).count("putter") == 1
        assert list(source.values()).count("driver") == 1
