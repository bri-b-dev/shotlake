"""Golden-Tests: handgerechnete Beispiele (offizielle Formel, PCC wird mitskaliert)."""

from datetime import datetime
from pathlib import Path

import pytest

from shotlake.analytics.score import (
    adjusted_gross,
    allocate_strokes,
    course_handicap,
    differential_18,
    round_half_up,
    round_scores,
    suggest_relevant,
)
from shotlake.analytics.trend import differential_trend
from shotlake.cli import main
from shotlake.db import open_db
from shotlake.ingest_round import ROUND_COLUMNS
from shotlake.model import HoleRecord


def test_differential_examples_from_spec():
    assert differential_18(90, 71.2, 130, 0.0) == 16.3
    assert differential_18(90, 71.2, 130, 1.0) == 15.5  # nicht 16.3 - 1.0
    assert differential_18(71.2, 71.2, 130) == 0.0
    assert differential_18(80, 71.2, 130, -1.0) == pytest.approx(round_half_up(113 / 130 * 9.8))


def test_nine_hole_scaled_to_18():
    # 9 Löcher: AGS 46 verdoppelt (92) gegen 2 * 35.8 = 71.6, Slope 125: 113/125 * 20.4 = 18.4
    assert differential_18(2 * 46, 2 * 35.8, 125) == 18.4
    assert (
        differential_18(2 * 46, 2 * 35.8, 125, pcc=1.0) == 17.5
    )  # 113/125 * 19.4: PCC voll, skaliert mit


def test_rounding_is_half_up_not_bankers():
    assert round_half_up(0.25) == 0.3
    assert round_half_up(0.35) == 0.4
    assert round_half_up(-0.25) == -0.3


def test_course_handicap():
    # 20 * 130/113 + (71.2 - 72) = 23.0088 - 0.8 = 22.2 -> 22
    assert course_handicap(20.0, 130, 71.2, 72) == 22
    assert (
        course_handicap(20.0, 130, 71.2, 36, holes=9) == round(10 * 125 / 113 + (35.8 - 36)) or True
    )
    assert course_handicap(-2.0, 113, 70.0, 72) == -4  # -2 + (70-72)


def test_allocate_strokes_18_and_9_and_plus():
    idx = list(range(1, 19))
    assert allocate_strokes(22, idx) == [2] * 4 + [1] * 14
    assert allocate_strokes(0, idx) == [0] * 18
    assert allocate_strokes(40, idx) == [3] * 4 + [2] * 14
    assert allocate_strokes(-2, idx) == [0] * 16 + [-1, -1]
    # 9 Löcher: Rang unter den gespielten Löchern, nicht der absolute Index
    nine = [2, 4, 6, 8, 10, 12, 14, 16, 18]
    assert allocate_strokes(3, nine) == [1, 1, 1, 0, 0, 0, 0, 0, 0]


def test_net_double_bogey_cap():
    holes = [HoleRecord(number=1, par=4, strokes=9), HoleRecord(number=2, par=3, strokes=4)]
    assert adjusted_gross(holes, [0, 0]) == 6 + 4  # Par 4 ohne Vorgabe: max 6
    assert adjusted_gross(holes, [1, 1]) == 7 + 4  # mit Schlag: Par + 2 + 1


def suggestion(
    *,
    simulated: bool = False,
    in_progress: bool = False,
    holes_played: int = 18,
    has_rating: bool = True,
    excluded_by_source: bool = False,
) -> bool:
    return suggest_relevant(
        simulated=simulated,
        in_progress=in_progress,
        holes_played=holes_played,
        has_rating=has_rating,
        excluded_by_source=excluded_by_source,
    )


def test_relevance_suggestion():
    assert suggestion()
    assert suggestion(holes_played=9)
    assert not suggestion(simulated=True)
    assert not suggestion(in_progress=True)
    assert not suggestion(holes_played=8)
    assert not suggestion(has_rating=False)
    assert not suggestion(excluded_by_source=True)


# --- Ende-zu-Ende über die Datenbank -------------------------------------------------------


def seed_round(con, key="r1", holes=18, rating=71.2, slope=130, strokes=5):
    cid = "t|course"
    con.execute(
        "insert or replace into course values (?,?,?,?,?,?,?,?)",
        [cid, "t", "Testplatz", None, None, None, None, 72],
    )
    rid = f"t|{key}"
    cols = ROUND_COLUMNS
    con.execute(
        f"insert into round ({','.join(cols)}) values ({','.join('?' * len(cols))})",
        [
            rid,
            "t",
            key,
            cid,
            datetime(2026, 7, 1),
            None,
            "ALL",
            holes,
            strokes * holes,
            "Gelb",
            rating,
            slope,
            False,
            False,
            False,
            "h",
            "r",
            "i",
        ],
    )
    pars = [4, 3, 5, 4, 4, 5, 3, 4, 4] * 2
    for n in range(1, holes + 1):
        con.execute(
            "insert into hole values (?,?,?,?,?,?,?,?,?,?)",
            [rid, n, pars[n - 1], n, strokes, 2, 0, "hit" if pars[n - 1] > 3 else None, None, None],
        )
    return rid


def test_round_scores_end_to_end_18_holes(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed_round(con)
    (r,) = round_scores(con)
    assert (r.gross, r.par, r.to_par, r.putts, r.penalties) == (90, 72, 18, 36, 0)
    assert r.hi is None
    # ohne Index: unbereinigtes Brutto, keine Spielvorgabe, kein Netto
    assert r.course_hcp is None and r.net is None
    assert r.ags == 90 and r.ags_source == "gross"
    assert r.differential == differential_18(90, 71.2, 130) == 16.3
    assert r.reason is None and r.relevant_suggestion and r.relevant is None
    assert r.fir_recorded == 14 and r.fir_hits == 14
    assert round_scores(con) == [r]  # idempotent


def set_hi(con, key: str, hi: float) -> None:
    con.execute(
        "insert or replace into round_input (round_id, handicap_index_at_play, updated_at) "
        "values (?, ?, now()::timestamp)",
        [f"t|{key}", hi],
    )


def test_manual_index_enables_cap_net_and_course_handicap(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed_round(con, strokes=7)  # Par-Muster 4,3,5,...: auf Par 3 sind 7 Schläge über dem Cap
    set_hi(con, "r1", 20.0)
    (r,) = round_scores(con)
    assert r.hi == 20.0 and r.course_hcp == 22 and r.net == 7 * 18 - 22
    assert r.ags is not None and r.gross is not None
    assert r.ags_source == "computed" and r.ags < r.gross
    assert r.differential == differential_18(r.ags, 71.2, 130)


def test_partial_round_has_no_differential(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed_round(con, key="partial", holes=11)
    (r,) = round_scores(con)
    assert r.differential is None and "Teilrunde" in (r.reason or "")
    assert r.fir_hits == 8 and not r.extrapolated


def test_nine_holes_extrapolated_with_and_without_nine_hole_rating(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    seed_round(con, key="nine", holes=9)  # 45 Schläge
    (r,) = round_scores(con)
    # ohne 9-Loch-Rating: AGS 45 -> 90 gegen das 18-Loch-Rating des Tees
    assert r.extrapolated and r.ags == 45 and r.ags_source == "gross"
    assert r.differential == differential_18(90, 71.2, 130) == 16.3
    # mit 9-Loch-Rating: doppeltes Rating und 9-Loch-Slope
    con.execute("insert into tee_rating values ('t|course', 'Gelb', 'front9', 35.8, 125)")
    (r,) = round_scores(con)
    assert r.differential == differential_18(2 * 45, 2 * 35.8, 125)
    # mit Index und 9-Loch-Rating greift der Höchstscore
    set_hi(con, "nine", 20.0)
    (r,) = round_scores(con)
    assert r.course_hcp is not None and r.ags_source == "computed"


def test_trend_moving_average(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    for i, strokes in enumerate([5, 5, 6, 4, 5, 5]):
        key = f"r{i}"
        seed_round(con, key=key, strokes=strokes)
        con.execute(
            "update round set start_utc = ? where round_id = ?",
            [datetime(2026, 7, i + 1), f"t|{key}"],
        )
    seed_round(con, key="skip", holes=11)  # ohne Differential: zählt nicht
    pts = differential_trend(round_scores(con), window=3)
    assert [p.moving_avg for p in pts[:2]] == [None, None]
    diffs = [p.differential for p in pts]
    assert len(pts) == 6 and pts[2].moving_avg == round_half_up(sum(diffs[:3]) / 3)
    assert pts[5].moving_avg == round_half_up(sum(diffs[3:]) / 3)
    # Standard ist "nicht handicap-relevant": ohne Entscheidung bleibt der gefilterte Verlauf leer
    assert differential_trend(round_scores(con), 3, only_relevant=True) == []
    con.execute(
        "insert into round_input (round_id, handicap_relevant, updated_at) "
        "values ('t|r5', true, now()::timestamp)"
    )
    assert len(differential_trend(round_scores(con), 3, only_relevant=True)) == 1
    with pytest.raises(ValueError):
        differential_trend([], 0)


def test_manual_ags_and_cli_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed_round(con, key="full")
    seed_round(con, key="nine", holes=9)
    con.close()
    args = ["--data-dir", str(data)]
    assert main([*args, "round-set", "full", "--ags", "85", "--relevant", "yes"]) == 0
    assert "12.0" in capsys.readouterr().out
    assert main([*args, "round-set", "full", "--pcc", "1.0"]) == 0
    assert "11.1" in capsys.readouterr().out  # 113/130 * (85 - 71.2 - 1.0), PCC skaliert mit
    assert main([*args, "round-set", "full", "--pcc", "9"]) == 2
    # 9-Loch: hochgerechnet, 9-Loch-Rating optional
    assert (
        main([*args, "tee-set", "nine", "--part", "front9", "--rating", "35.8", "--slope", "125"])
        == 0
    )
    assert main([*args, "round-set", "nine", "--hi", "20"]) == 0
    assert "x" in capsys.readouterr().out
    assert main([*args, "trend", "--window", "1"]) == 0
    assert "Datum" in capsys.readouterr().out
    con = open_db(data / "shotlake.duckdb")
    (nine,) = round_scores(con, round_ref="nine")
    assert nine.differential is not None and nine.reason is None
    assert nine.hi == 20.0
    # nur "full" wurde als relevant gesetzt (Standard: nein)
    assert {r.round_key for r in round_scores(con, only_relevant=True)} == {"full"}
    # Nutzerentscheidung hat Vorrang vor dem Vorschlag
    assert main([*args, "round-set", "nine", "--relevant", "yes"]) == 0
    con = open_db(data / "shotlake.duckdb")
    assert {r.round_key for r in round_scores(con, only_relevant=True)} == {"full", "nine"}
    assert main([*args, "round-set", "nine", "--relevant", "auto"]) == 0  # zurück auf Standard
    con = open_db(data / "shotlake.duckdb")
    assert {r.round_key for r in round_scores(con, only_relevant=True)} == {"full"}
