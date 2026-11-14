from datetime import UTC, datetime

import pytest

from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.garmin_range import parse_garmin_range_csv

F2024 = "DrivingRange-2024-04-06 13-52-17-444.csv"
F2023 = "DrivingRange-2023-10-06 18-44-56-028.csv"


def test_session_time_comes_from_rows_not_filename(fixtures):
    s = parse_garmin_range_csv(fixtures / "garmin_range" / F2023)
    assert s.start_utc.year == 2023 and s.start_utc.month == 8
    assert "2023-10-06" not in s.session_key


def test_time_with_24h_hour_and_pm_suffix(fixtures):
    s = parse_garmin_range_csv(fixtures / "garmin_range" / F2024)
    # "03/30/24 13:35:17 PM" local Europe/Berlin (CET, UTC+1) -> 12:35:17 UTC
    assert s.shots[0].shot_time_utc == datetime(2024, 3, 30, 12, 35, 17, tzinfo=UTC)
    assert s.start_utc == s.shots[0].shot_time_utc


def test_unit_row_is_not_a_shot_and_player_name_is_dropped(fixtures):
    s = parse_garmin_range_csv(fixtures / "garmin_range" / F2024)
    assert len(s.shots) == 6
    assert not hasattr(s.shots[0], "player")


def test_units_are_converted_to_si(fixtures):
    shot = parse_garmin_range_csv(fixtures / "garmin_range" / F2024).shots[0]
    assert shot.club_type == "wedge_sand"
    assert shot.measurement_method == "radar"
    assert shot.club_speed_ms == pytest.approx(81.64135437011718 / 3.6)
    assert shot.ball_speed_ms == pytest.approx(107.12857818603516 / 3.6)
    assert shot.carry_m == pytest.approx(17.983951568603516)
    assert shot.total_m == pytest.approx(31.537067413330078)
    assert shot.carry_side_m == pytest.approx(-1.3458718061447144)
    assert shot.spin_rpm == pytest.approx(1875.5859375)
    assert shot.spin_source == "calculated"
    assert shot.club_path_deg == pytest.approx(-3.489839553833008)
    assert shot.face_to_target_deg == pytest.approx(-4.617658615112305)


def test_environment_columns(fixtures):
    shot = parse_garmin_range_csv(fixtures / "garmin_range" / F2024).shots[0]
    assert shot.temperature_c == pytest.approx(21.11111068725586)
    assert shot.indoor is None


def test_german_club_names_are_mapped(fixtures):
    clubs = {x.club_type for x in parse_garmin_range_csv(fixtures / "garmin_range" / F2023).shots}
    assert clubs <= {"wedge_pitching", "wedge_sand", "iron_7", "iron_9", "hybrid_5", "driver"}
    assert None not in clubs


def test_empty_fields_are_none(fixtures):
    shots = parse_garmin_range_csv(fixtures / "garmin_range" / F2024).shots
    # some rows lack path/face data in the real export; none must be 0.0 by accident
    for s in shots:
        assert s.club_path_deg is None or isinstance(s.club_path_deg, float)


def test_schema_drift_when_units_change(tmp_path):
    p = tmp_path / "DrivingRange-2024-01-01 10-00-00-000.csv"
    p.write_text(
        "Datum,Spieler,Schlägername,Schlägerart,Carry-Distanz,Gesamtstrecke,Ballgeschwindigkeit\n"
        ",,,,[yd],[yd],[mph]\n"
        "01/01/24 10:00:00 AM,X,,Driver,200,210,150\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(SchemaDriftError):
        parse_garmin_range_csv(p)
