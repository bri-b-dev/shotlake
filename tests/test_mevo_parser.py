from datetime import UTC, datetime

import pytest

from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.mevo import parse_mevo_csv


def test_session_time_from_filename(fixtures):
    s = parse_mevo_csv(fixtures / "mevo" / "08-may-2026_05_17_pm.csv")
    # 08 May 2026 17:17 Europe/Berlin (CEST, UTC+2) -> 15:17 UTC
    assert s.start_utc == datetime(2026, 5, 8, 15, 17, tzinfo=UTC)
    assert s.session_key == "08-may-2026_05_17_pm"
    assert s.source == "mevo"


def test_morning_filename(fixtures):
    s = parse_mevo_csv(fixtures / "mevo" / "25-may-2026_11_21_am.csv")
    assert s.start_utc == datetime(2026, 5, 25, 9, 21, tzinfo=UTC)


def test_avg_and_dev_rows_are_not_shots(fixtures):
    s = parse_mevo_csv(fixtures / "mevo" / "08-may-2026_05_17_pm.csv")
    assert [x.shot_key for x in s.shots] == ["5 Wood|1", "5 Wood|2"]


def test_ragged_avg_rows_do_not_break_parsing(fixtures):
    s = parse_mevo_csv(fixtures / "mevo" / "25-may-2026_11_21_am.csv")
    assert len(s.shots) == 3
    assert all(x.club_raw == "Driver" for x in s.shots)


def test_units_and_signs_are_converted(fixtures):
    shot = parse_mevo_csv(fixtures / "mevo" / "08-may-2026_05_17_pm.csv").shots[0]
    assert shot.club_type == "wood_5"
    assert shot.measurement_method == "radar"
    assert shot.ball_speed_ms == pytest.approx(85 * 0.44704)
    assert shot.carry_m == pytest.approx(106.8 * 0.9144)
    assert shot.total_m == pytest.approx(117.8 * 0.9144)
    assert shot.spin_rpm == 3514
    assert shot.apex_m == pytest.approx(40.5 * 0.3048)
    # "4.7 R" -> right = positive; "0.5 L" -> left = negative
    assert shot.carry_side_m == pytest.approx(4.7 * 0.9144)
    assert shot.launch_direction_deg == pytest.approx(-0.5)
    assert shot.spin_axis_deg == pytest.approx(22.3)
    assert shot.launch_angle_deg == pytest.approx(22.5)
    assert shot.attack_angle_deg == pytest.approx(-3.9)
    assert shot.club_path_deg == pytest.approx(-3.3)
    assert shot.face_to_target_deg == pytest.approx(0.7)
    assert shot.face_to_path_deg == pytest.approx(4.0)
    assert shot.indoor is False
    assert shot.temperature_c == pytest.approx(25.0)


def test_missing_marker_becomes_none(fixtures):
    shot = parse_mevo_csv(fixtures / "mevo" / "08-may-2026_05_17_pm.csv").shots[0]
    # wind columns are "- *"
    assert shot.wind_speed_ms is None


def test_older_export_variant_leaves_new_columns_null(fixtures):
    s = parse_mevo_csv(fixtures / "mevo" / "07-sep-2025_05_13_pm.csv")
    assert len(s.shots) >= 1
    shot = s.shots[0]
    assert shot.carry_m is not None
    assert shot.club_path_deg is None
    assert shot.face_to_target_deg is None
    assert shot.attack_angle_deg is not None


def test_unknown_club_is_flagged_not_guessed(tmp_path):
    p = tmp_path / "01-jan-2026_10_00_am.csv"
    p.write_text(
        "club,Shot,Ball (mph),Carry (yds),Total (yds)\nMystery Stick,1,100,150,160\n",
        encoding="utf-8",
    )
    shot = parse_mevo_csv(p).shots[0]
    assert shot.club_type is None
    assert shot.club_raw == "Mystery Stick"
    assert "club_unmapped" in shot.flags


def test_schema_drift_when_required_column_missing(tmp_path):
    p = tmp_path / "01-jan-2026_10_00_am.csv"
    p.write_text("club,Shot,Ball (km/h),Carry (m),Total (m)\nDriver,1,160,200,210\n")
    with pytest.raises(SchemaDriftError):
        parse_mevo_csv(p)
