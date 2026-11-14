"""Strokes Gained: Golden-Beispiel mit handgerechneter Baseline, Invarianten, Baseline-Datei."""

from pathlib import Path

import pytest

from shotlake.analytics.sg import _Hole, _Shot, hole_sg, strokes_gained
from shotlake.baseline import Baseline, default_baseline_path, load_baseline
from shotlake.cli import main
from shotlake.db import open_db
from shotlake.geo import haversine_m

M_PER_DEG = haversine_m(0.0, 0.0, 1.0, 0.0)  # Meter je Breitengrad (Kugelmodell)

# Testbaseline: E = 1 + d/100 für Tee/Fairway/Rough/Sand, Grün 1 + d/10 (bis 10 m: 1..2)
LINEAR = Baseline(
    id="test-linear",
    status="test",
    description="",
    tables={
        "tee": ((0.0, 500.0), (1.0, 6.0)),
        "fairway": ((0.0, 500.0), (1.0, 6.0)),
        "rough": ((0.0, 500.0), (1.5, 6.5)),
        "sand": ((0.0, 500.0), (2.0, 7.0)),
        "green": ((0.0, 10.0), (1.0, 2.0)),
    },
)
PIN = (50.0, 8.0)


def at(distance_m: float) -> tuple[float, float]:
    """Punkt in `distance_m` südlich der Fahne."""
    return (PIN[0] - distance_m / M_PER_DEG, PIN[1])


def shot(order, start_lie, d_start, end_lie, d_end, excluded=False):
    return _Shot(order, start_lie, end_lie, at(d_start), at(d_end), excluded)


def hole(strokes, putts, penalties=0, par=4, pin=PIN):
    return _Hole("r", 1, par, strokes, putts, penalties, pin)


def test_interpolation_and_extrapolation():
    assert LINEAR.expected("fairway", 100) == pytest.approx(2.0)
    assert LINEAR.expected("green", 5) == pytest.approx(1.5)
    assert LINEAR.expected("tee", 600) == pytest.approx(7.0)  # lineare Fortsetzung
    assert LINEAR.expected("green", -3) == 1.0
    assert LINEAR.expected("other", 50) is None


def test_golden_hole_par4():
    # Tee 300 m (E=4.0) -> Fairway 100 m (E=2.0) -> Grün 5 m (E=1.5) -> 2 Putts. Score 4.
    h, shots = hole_sg(
        hole(4, 2),
        [shot(1, "tee", 300, "fairway", 100), shot(2, "fairway", 100, "green", 5)],
        LINEAR,
    )
    assert h.complete and h.reason is None
    assert h.by_category["tee"] == pytest.approx(1.0)  # 4.0 - 2.0 - 1
    assert h.by_category["approach"] == pytest.approx(-0.5)  # 2.0 - 1.5 - 1
    assert h.by_category["putting"] == pytest.approx(-0.5)  # 1.5 - 2
    assert h.total == pytest.approx(0.0, abs=1e-6)  # 4.0 - 4
    assert sum(h.by_category.values()) == pytest.approx(h.total or 0.0)  # Summe = Gesamt
    assert [s.category for s in shots] == ["tee", "approach"]


def test_penalty_and_short_game_categories():
    # Tee 200 m (E=3.0) -> Rough 40 m (E=1.9) -> Grün 2 m; 1 Putt, 1 Strafschlag, Score 4
    h, shots = hole_sg(
        hole(4, 1, penalties=1, par=3),
        [shot(1, "tee", 200, "rough", 40), shot(2, "rough", 40, "green", 2)],
        LINEAR,
    )
    assert h.complete
    # Par 3: Tee-Schlag zählt als Approach (>= 90 m); zweiter Schlag kurzes Spiel
    assert [s.category for s in shots] == ["approach", "short"]
    assert h.by_category["penalty"] == -1.0
    assert sum(h.by_category.values()) == pytest.approx(h.total or 0.0)
    assert h.total == pytest.approx(3.0 - 4, abs=1e-6)


def test_holed_chip_has_no_putting():
    shots = [shot(1, "tee", 300, "fairway", 20), shot(2, "fairway", 20, "green", 0)]
    h, _ = hole_sg(hole(2, 0), shots, LINEAR)
    assert h.complete and "putting" not in h.by_category
    assert h.total == pytest.approx(4.0 - 2, abs=1e-6)
    assert h.total == pytest.approx(sum(h.by_category.values()), abs=1e-6)


def test_recorded_putt_shots_give_real_first_putt_distance():
    # Arccos: Putts sind Schläge. Approach endet 5 m vor der Fahne, 2 Putts (E(5 m) = 1.5)
    putts = [shot(3, "green", 5, "green", 1), shot(4, "green", 1, "green", 0)]
    h, shots = hole_sg(
        hole(4, 2),
        [shot(1, "tee", 300, "fairway", 100), shot(2, "fairway", 100, "green", 5), *putts],
        LINEAR,
    )
    assert h.complete
    assert h.by_category["putting"] == pytest.approx(1.5 - 2)
    assert "green_play" not in h.by_category
    assert [s.category for s in shots] == ["tee", "approach"]  # Putts nur in der Kategorie putting
    assert sum(h.by_category.values()) == pytest.approx(h.total or 0.0, abs=1e-6)
    # Anzahl der Putt-Schläge muss zur Putt-Zahl passen
    h2, _ = hole_sg(
        hole(4, 2),
        [shot(1, "tee", 300, "fairway", 100), shot(2, "fairway", 100, "green", 5), putts[0]],
        LINEAR,
    )
    assert not h2.complete and "Putt-Schläge" in (h2.reason or "")


def test_snapped_green_end_merges_last_shot_and_putts():
    # Ende des Approach liegt auf der Fahne (0 m): Putt-Distanz unbekannt, Putts nicht trennbar
    h, shots = hole_sg(
        hole(4, 2),
        [shot(1, "tee", 300, "fairway", 100), shot(2, "fairway", 100, "green", 0)],
        LINEAR,
    )
    assert h.complete
    assert "putting" not in h.by_category
    assert h.by_category["green_play"] == pytest.approx(2.0 - 2 - 1)  # E(Start) - Putts - 1
    assert h.by_category["tee"] == pytest.approx(1.0)
    assert sum(h.by_category.values()) == pytest.approx(h.total or 0.0, abs=1e-6)
    assert [s.category for s in shots] == ["tee", "green_play"]


@pytest.mark.parametrize(
    ("h", "shots", "reason"),
    [
        (hole(5, 2), [shot(1, "tee", 300, "fairway", 100)], "erwartet"),  # Schlag fehlt
        (hole(None, None), [], "nicht erfasst"),
        (hole(4, 2, pin=None), [shot(1, "tee", 300, "fairway", 100)] * 2, "Fahnenposition"),
        (
            hole(4, 2),
            [
                shot(1, "tee", 300, "fairway", 100),
                shot(2, "fairway", 100, "green", 5, excluded=True),
            ],
            "ausgeschlossen",
        ),
        (
            hole(4, 2),
            [shot(1, "tee", 300, None, 100), shot(2, "fairway", 100, "green", 5)],
            "Lage oder Koordinaten",
        ),
        (
            hole(4, 2),
            [shot(1, "fairway", 300, "fairway", 100), shot(2, "fairway", 100, "green", 5)],
            "nicht vom Tee",
        ),
        (
            hole(4, 2),
            [shot(1, "tee", 300, "fairway", 100), shot(2, "fairway", 100, "rough", 5)],
            "nicht auf dem Grün",
        ),
        (
            hole(4, 2),
            [shot(1, "tee", 300, "other", 100), shot(2, "other", 100, "green", 5)],
            "Baseline",
        ),
    ],
)
def test_incomplete_holes_are_reported_not_guessed(h, shots, reason):
    result, out = hole_sg(h, shots, LINEAR)
    assert not result.complete and result.total is None and out == []
    assert reason in (result.reason or "")


def test_real_baseline_file_is_valid_and_plausible():
    base = load_baseline(Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml")
    assert base.id == "hcp18w-approx-v1" and base.status == "approximation"
    for dist in (20, 60, 100, 150, 250):
        # Rough nie leichter als Fairway, Sand nie leichter als Rough
        f, r, s = (base.expected(x, dist) for x in ("fairway", "rough", "sand"))
        assert f is not None and r is not None and s is not None and f < r < s + 1e-9
    e300 = base.expected("tee", 300)
    assert e300 is not None and 4.5 < e300 < 5.2  # Par 4 mit Handicap 18 liegt um 5 Schläge
    assert default_baseline_path().name.endswith(".toml")


def test_baseline_validation(tmp_path: Path):
    bad = tmp_path / "b.toml"
    bad.write_text(
        '[meta]\nid = "x"\nstatus = "a"\n[tables.tee]\ndistance_m=[1,2]\nexpected=[1,2]\n'
    )
    with pytest.raises(ValueError, match="Quelle und Lizenz"):
        load_baseline(bad)
    bad.write_text(
        '[meta]\nid="x"\nstatus="a"\nsource="s"\nlicense="l"\n'
        "[tables.tee]\ndistance_m=[2,1]\nexpected=[1,2]\n"
    )
    with pytest.raises(ValueError, match="aufsteigen"):
        load_baseline(bad)


# --- Datenbank und CLI ------------------------------------------------------------------


def seed_db(con):
    con.execute(
        "insert into round (round_id, source, round_key, course_id, start_utc, holes_played, "
        "in_progress, exclude_from_stats, simulated, content_hash, raw_hash, import_run_id) "
        "values ('t|r', 't', 'r', 'c', '2026-07-01', 1, false, false, false, 'h', 'r', 'i')"
    )
    con.execute(
        "insert into hole values ('t|r', 1, 4, NULL, 4, 2, 0, NULL, ?, ?)", [PIN[0], PIN[1]]
    )
    con.execute(
        "insert into hole values ('t|r', 2, 4, NULL, 5, 2, 0, NULL, ?, ?)", [PIN[0], PIN[1]]
    )
    cols = [
        "shot_id",
        "round_id",
        "hole_number",
        "shot_order",
        "start_lat",
        "start_lon",
        "start_lie",
        "end_lat",
        "end_lon",
        "end_lie",
        "exclude_from_stats",
        "content_hash",
        "raw_hash",
        "import_run_id",
    ]
    for order, (sl, sd, el, ed) in enumerate(
        [("tee", 300, "fairway", 100), ("fairway", 100, "green", 5)], start=1
    ):
        sa, ea = at(sd), at(ed)
        con.execute(
            f"insert into course_shot ({','.join(cols)}) values ({','.join('?' * len(cols))})",
            [
                f"s{order}",
                "t|r",
                1,
                order,
                sa[0],
                sa[1],
                sl,
                ea[0],
                ea[1],
                el,
                False,
                "h",
                "r",
                "i",
            ],
        )  # Loch 2 hat Score 5, aber keinen erfassten Schlag -> unvollständig


def test_strokes_gained_from_database_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed_db(con)
    res = strokes_gained(con, LINEAR)
    assert res.complete_holes == 1 and len(res.holes) == 2
    assert res.totals()["tee"] == pytest.approx(1.0)
    assert res.baseline_id == "test-linear"
    con.close()
    base = tmp_path / "b.toml"
    base.write_text(
        '[meta]\nid="test-linear"\nstatus="test"\nsource="s"\nlicense="l"\n'
        + "".join(
            f"[tables.{lie}]\ndistance_m=[0,500]\nexpected=[{lo},{hi}]\n"
            for lie, lo, hi in [
                ("tee", 1, 6),
                ("fairway", 1, 6),
                ("rough", 1.5, 6.5),
                ("sand", 2, 7),
            ]
        )
        + "[tables.green]\ndistance_m=[0,10]\nexpected=[1,2]\n"
    )
    assert main(["--data-dir", str(data), "sg", "--baseline", str(base), "--reasons"]) == 0
    out = capsys.readouterr().out
    assert "test-linear" in out and "1 von 2" in out and "Abschlag" in out and "erwartet" in out
