from datetime import date

import pytest

from shotlake.analytics.distance import club_stats, gapping
from shotlake.db import open_db
from shotlake.ingest import import_file

HEADER = "club,Shot,Ball (mph),Carry (yds),Total (yds),Lateral (yds),Mode\n"


def write_mevo(tmp_path, day, rows, mode="Outdoor", month="jan"):
    """rows: (club, carry_yd, total_yd, lateral_yd)"""
    p = tmp_path / f"{day:02d}-{month}-2026_10_00_am.csv"
    lines = [HEADER]
    for i, (club, carry, total, lat) in enumerate(rows, 1):
        lat_s = "-" if lat is None else (f"{abs(lat)} {'R' if lat >= 0 else 'L'}")
        lines.append(f"{club},{i},100,{carry},{total},{lat_s},{mode}\n")
    p.write_text("".join(lines), encoding="utf-8")
    return p


@pytest.fixture
def con():
    c = open_db(":memory:")
    yield c
    c.close()


def load(con, path, source="mevo"):
    import_file(con, source, path, raw_dir=None)


def test_basic_stats_per_club(con, tmp_path):
    rows = [("7 Iron", 100 + i, 110 + i, 0.0) for i in range(10)]
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="iron_7")
    assert s.n == 10
    assert s.carry_median_m == pytest.approx(104.5 * 0.9144)
    assert s.carry_mean_m == pytest.approx(104.5 * 0.9144)
    assert s.carry_p10_m is not None
    assert s.carry_median_m is not None
    assert s.carry_p90_m is not None
    assert s.carry_p10_m < s.carry_median_m < s.carry_p90_m
    assert s.total_median_m == pytest.approx(114.5 * 0.9144)
    assert s.warning is None


def test_min_sample_warning(con, tmp_path):
    rows = [("7 Iron", 100 + i, 110 + i, 0.0) for i in range(4)]
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="iron_7", min_n=10)
    assert s.n == 4
    assert s.warning == "insufficient_data"


def test_outliers_excluded_by_default(con, tmp_path):
    rows = [("Driver", 200, 210, 0.0)] * 9 + [("Driver", 900, 910, 0.0)]
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="driver")
    assert s.n == 9
    [s_all] = club_stats(con, club_type="driver", include_flagged=True)
    assert s_all.n == 10


def test_side_bias_and_shares(con, tmp_path):
    # 6 rechts (+10 yd), 2 links (-10 yd), 2 mittig (0)
    rows = [("Driver", 200, 210, 10.0)] * 6 + [("Driver", 200, 210, -10.0)] * 2
    rows += [("Driver", 200, 210, 0.0)] * 2
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="driver", corridor_m=5.0)
    assert s.side_bias_m == pytest.approx(4 * 0.9144)  # (60-20)/10 yd
    assert s.right_share == pytest.approx(0.6)
    assert s.left_share == pytest.approx(0.2)
    assert s.center_share == pytest.approx(0.2)
    assert s.n_side == 10


def test_missing_side_values_are_not_zero(con, tmp_path):
    rows = [("Driver", 200, 210, None)] * 5 + [("Driver", 200, 210, 10.0)] * 5
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="driver")
    assert s.n == 10 and s.n_side == 5
    assert s.side_bias_m == pytest.approx(10 * 0.9144)


def test_sources_are_separate_by_default_and_marked_when_combined(con, tmp_path, fixtures):
    load(con, write_mevo(tmp_path, 1, [("Sand Wedge", 60 + i, 70 + i, 0.0) for i in range(10)]))
    load(
        con,
        fixtures / "garmin_range" / "DrivingRange-2024-04-06 13-52-17-444.csv",
        source="garmin_range",
    )
    split = club_stats(con, club_type="wedge_sand", min_n=1)
    assert {s.source for s in split} == {"mevo", "garmin_range"}
    combined = club_stats(con, club_type="wedge_sand", min_n=1, combine=True)
    assert len(combined) == 1 and combined[0].mixed is True
    assert combined[0].n == sum(s.n for s in split)


def test_date_filter(con, tmp_path):
    load(con, write_mevo(tmp_path, 1, [("Driver", 200, 210, 0.0)] * 5))
    load(con, write_mevo(tmp_path, 20, [("Driver", 220, 230, 0.0)] * 5))
    [s] = club_stats(con, club_type="driver", since=date(2026, 1, 10), min_n=1)
    assert s.carry_median_m == pytest.approx(220 * 0.9144)


def test_indoor_filter(con, tmp_path):
    load(con, write_mevo(tmp_path, 1, [("Driver", 200, 210, 0.0)] * 5, mode="Outdoor"))
    load(con, write_mevo(tmp_path, 2, [("Driver", 150, 160, 0.0)] * 5, mode="Indoor"))
    [s] = club_stats(con, club_type="driver", indoor=False, min_n=1)
    assert s.carry_median_m == pytest.approx(200 * 0.9144)


def test_gapping_flags_overlap_and_ok_gaps(con, tmp_path):
    rows = (
        [("7 Iron", 150, 160, 0.0)] * 5
        + [("8 Iron", 138, 148, 0.0)] * 5
        + [("9 Iron", 136, 146, 0.0)] * 5
    )
    load(con, write_mevo(tmp_path, 1, rows))
    gaps = gapping(con, min_n=3)
    by_pair = {(g.longer_club, g.shorter_club): g for g in gaps}
    assert by_pair[("iron_7", "iron_8")].gap_m == pytest.approx(12 * 0.9144)
    assert by_pair[("iron_7", "iron_8")].status == "ok"
    assert by_pair[("iron_8", "iron_9")].status == "overlap"


def test_gapping_skips_clubs_with_too_few_shots(con, tmp_path):
    rows = [("7 Iron", 150, 160, 0.0)] * 5 + [("8 Iron", 140, 150, 0.0)] * 2
    load(con, write_mevo(tmp_path, 1, rows))
    assert gapping(con, min_n=3) == []


def test_gapping_orders_by_carry_not_by_name(con, tmp_path):
    rows = [("9 Iron", 120, 130, 0.0)] * 5 + [("Driver", 220, 230, 0.0)] * 5
    load(con, write_mevo(tmp_path, 1, rows))
    [g] = gapping(con, min_n=3)
    assert (g.longer_club, g.shorter_club) == ("driver", "iron_9")
    assert g.status == "gap_too_large"


def test_confidence_intervals_for_carry_median_and_side_bias(con, tmp_path):
    rows = [("Driver", 200 + i, 210 + i, 6.0 + (i % 3)) for i in range(40)]
    load(con, write_mevo(tmp_path, 1, rows))
    [s] = club_stats(con, club_type="driver")
    assert s.carry_median_ci is not None and s.side_bias_ci is not None
    assert s.carry_median_m is not None and s.side_bias_m is not None
    assert s.carry_median_ci.low <= s.carry_median_m <= s.carry_median_ci.high
    assert 0 < s.side_bias_ci.low < s.side_bias_m < s.side_bias_ci.high  # Bias rechts belegt
    few = [("7 Iron", 100 + i, 110 + i, 1.0) for i in range(9)]
    load(con, write_mevo(tmp_path, 2, few))
    [i7] = club_stats(con, club_type="iron_7")
    assert (
        i7.carry_median_ci is None and i7.side_bias_ci is None
    )  # unter 10 Schlägen: keine Aussage
