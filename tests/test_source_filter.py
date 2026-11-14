"""`--source`: dieselbe Runde in zwei Quellen darf nicht doppelt zählen, wenn gefiltert wird."""

from pathlib import Path

import pytest

from shotlake.analytics.course_shots import club_distances
from shotlake.analytics.score import round_scores
from shotlake.analytics.sg import strokes_gained
from shotlake.baseline import load_baseline
from shotlake.cli import main
from shotlake.db import open_db
from tests.test_course_shots import SHOTS, seed
from tests.test_score import seed_round

BASELINE = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"


def test_round_scores_filter_by_source(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed_round(con, key="a")
    seed_round(con, key="b")
    con.execute("update round set source = 'garmin_platform' where round_key = 'b'")
    assert len(round_scores(con)) == 2
    assert [r.round_key for r in round_scores(con, source="garmin_platform")] == ["b"]
    assert round_scores(con, source="arccos") == []


def test_course_shots_and_sg_filter_by_source(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed(con, SHOTS)  # Runde t|r mit source 't'
    assert club_distances(con, min_n=1)
    assert club_distances(con, min_n=1, source="t")
    assert club_distances(con, min_n=1, source="arccos") == []
    base = load_baseline(BASELINE)
    assert strokes_gained(con, base, source="arccos").holes == []


def test_cli_accepts_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed_round(con, key="a")
    con.close()
    args = ["--data-dir", str(data)]
    for cmd in ("rounds", "trend", "sg", "course-shots"):
        assert main([*args, cmd, "--source", "arccos"]) == 0
    out = capsys.readouterr().out
    assert "Datum" in out
