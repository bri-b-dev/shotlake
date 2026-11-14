"""Auswertung der Platzschläge (Distanz je Schläger, Lagen, Treffer)."""

from datetime import date
from pathlib import Path

from shotlake.analytics.course_shots import (
    approach_green_rate,
    club_distances,
    start_lies,
    tee_shot_outcomes,
)
from shotlake.cli import main
from shotlake.db import open_db

SHOT_COLS = [
    *("shot_id", "round_id", "hole_number", "shot_order", "club_raw", "shot_type"),
    *("distance_m", "start_lie", "end_lie", "exclude_from_stats"),
    *("content_hash", "raw_hash", "import_run_id"),
]


def seed(con, shots):
    con.execute(
        "insert into round (round_id, source, round_key, course_id, start_utc, holes_played, "
        "in_progress, exclude_from_stats, simulated, content_hash, raw_hash, import_run_id) "
        "values ('t|r', 't', 'r', 'c', '2026-07-01', 9, false, false, false, 'h', 'r', 'i')"
    )
    for i, (club, kind, dist, start, end, excl) in enumerate(shots, start=1):
        con.execute(
            f"insert into course_shot ({','.join(SHOT_COLS)}) values ({','.join('?' * 13)})",
            [f"s{i}", "t|r", 1, i, club, kind, dist, start, end, excl, "h", "r", "i"],
        )


SHOTS = [
    ("c1", "TEE", 200.0, "tee", "fairway", False),
    ("c1", "TEE", 180.0, "tee", "rough", False),
    ("c1", "TEE", 220.0, "tee", "fairway", True),  # von der Quelle ausgeschlossen
    ("c2", "APPROACH", 100.0, "fairway", "green", False),
    ("c2", "APPROACH", 90.0, "rough", "rough", False),
    ("c2", "APPROACH", 95.0, "fairway", None, False),  # Ziel-Lage unbekannt zählt nicht
    (None, "CHIP", 10.0, "rough", "green", False),  # ohne Schläger: keine Schlägerzeile
    ("c3", "UNKNOWN", 3.0, "green", "green", False),  # Putt-artig, vom Grün: ausgeschlossen
]


def test_club_distances_excludes_flagged_green_and_unclubbed(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed(con, SHOTS)
    rows = {c.club: c for c in club_distances(con, min_n=2)}
    assert set(rows) == {"c1", "c2"}
    assert rows["c1"].n == 2 and rows["c1"].median_m == 190.0 and rows["c1"].warning is None
    assert rows["c2"].n == 3 and not rows["c2"].mapped
    assert club_distances(con, min_n=3)[0].warning == "insufficient_data"


def test_club_map_renames_and_marks_mapped(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed(con, SHOTS)
    con.execute("insert into club_map values ('t', 'c1', 'driver')")
    rows = {c.club: c for c in club_distances(con, min_n=1)}
    assert rows["driver"].mapped and rows["c2"].mapped is False


def test_lies_tee_outcomes_and_green_rate(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed(con, SHOTS)
    outcomes = {t.lie: t for t in tee_shot_outcomes(con)}
    assert outcomes["fairway"].n == 1 and outcomes["rough"].n == 1
    assert abs(outcomes["fairway"].share - 0.5) < 1e-9
    lies = {s.lie: s.n for s in start_lies(con)}
    assert "green" not in lies and lies["tee"] == 2
    assert approach_green_rate(con) == {"fairway": (1, 1), "rough": (0, 1)}


def test_date_filter_and_cli(tmp_path: Path, capsys):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed(con, SHOTS)
    con.close()
    con = open_db(data / "shotlake.duckdb")
    assert club_distances(con, since=date(2026, 7, 2)) == []
    con.close()
    args = ["--data-dir", str(data)]
    assert main([*args, "club-set", "t", "c1", "driver"]) == 0
    assert main([*args, "course-shots", "--min-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "driver" in out and "nicht zugeordnet" in out and "kein Carry" in out
