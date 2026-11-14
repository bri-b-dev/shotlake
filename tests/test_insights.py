"""Insights: regelbasiert, deterministisch, mit Unsicherheit (synthetische Szenarien)."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotlake.analytics.insights import (
    bias_insights,
    gap_insights,
    generate,
    sg_insights,
    trend_insights,
)
from shotlake.analytics.metrics import Filters, sg_round_observations
from shotlake.analytics.sg import strokes_gained
from shotlake.baseline import load_baseline
from shotlake.cli import main
from shotlake.db import open_db
from shotlake.mcp_server import create_server
from shotlake.web.app import create_app
from shotlake.workspace import Workspace
from tests.test_sg import PIN, at

BASELINE_PATH = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"
BASELINE = load_baseline(BASELINE_PATH)

SHOT_COLS = [
    *("shot_id", "round_id", "hole_number", "shot_order", "start_lat", "start_lon", "start_lie"),
    *("end_lat", "end_lon", "end_lie", "shot_type", "exclude_from_stats"),
    *("content_hash", "raw_hash", "import_run_id"),
]


def add_round(con, n: int, when: datetime, putts: int = 2, holes: int = 6, source: str = "arccos"):
    """Runde aus `holes` Par-4-Löchern: Abschlag 300 m -> Fairway 100 m -> Grün 5 m -> Putts."""
    rid = f"{source}|r{n}"
    con.execute(
        "insert into round (round_id, source, round_key, course_id, start_utc, holes_played, "
        "in_progress, exclude_from_stats, simulated, content_hash, raw_hash, import_run_id) "
        "values (?, ?, ?, 'c', ?, ?, false, false, false, 'h', 'r', 'i')",
        [rid, source, f"r{n}", when, holes],
    )
    con.execute(
        "insert or ignore into course values ('c', ?, 'Testplatz', null, null, null, null, 72)",
        [source],
    )
    for hole in range(1, holes + 1):
        strokes = 2 + putts
        con.execute(
            "insert into hole values (?,?,?,?,?,?,?,?,?,?)",
            [rid, hole, 4, hole, strokes, putts, 0, None, PIN[0], PIN[1]],
        )
        moves = [("tee", 300, "fairway", 100), ("fairway", 100, "green", 5)]
        moves += [("green", 5 if k == 0 else 1, "green", 1 if k < putts - 1 else 0)
                  for k in range(putts)]  # fmt: skip
        for order, (sl, sd, el, ed) in enumerate(moves, start=1):
            sa, ea = at(sd), at(ed)
            con.execute(
                f"insert into course_shot ({','.join(SHOT_COLS)}) "
                f"values ({','.join('?' * len(SHOT_COLS))})",
                [f"{rid}|{hole}|{order}", rid, hole, order, sa[0], sa[1], sl, ea[0], ea[1], el,
                 None, False, "h", "r", "i"],
            )  # fmt: skip


@pytest.fixture
def con(tmp_path: Path):
    c = open_db(tmp_path / "t.duckdb")
    yield c
    c.close()


def seed_many(con, n: int, putts_by_round: list[int] | None = None, source: str = "arccos"):
    for i in range(n):
        putts = putts_by_round[i] if putts_by_round else 2
        add_round(con, i, datetime(2026, 6, 1 + i), putts=putts, source=source)


# --- Strokes Gained ---------------------------------------------------------------------------


def _sg(con, source="arccos"):
    res = strokes_gained(con, BASELINE, source=source)
    return sg_insights(
        BASELINE, source, res, sg_round_observations(con, Filters(source=source), BASELINE, res)
    )


def test_clear_putting_loss_is_reported_with_effect_ci_and_link(con):
    seed_many(con, 8, putts_by_round=[3] * 8)  # jedes Loch 3 Putts: E(5 m) 1,95 - 3 = -1,05
    putting = next(i for i in _sg(con) if i.id == "sg_loss:putting:arccos")
    assert putting.status == "belegt" and putting.n == 8
    assert putting.effect == pytest.approx(-1.05 * 18, abs=0.01)
    assert putting.ci is not None and putting.ci.high < 0
    assert "Putten kostet ca. 18,9 Schläge pro 18 Löcher" in putting.text
    assert "hcp18w-approx-v1" in putting.text and "n = 8 Runden" in putting.text
    assert putting.links["tool"] == "get_stats"
    assert putting.links["args"] == {"metric": "sg_putting_per_hole", "source": "arccos"}


def test_uncertain_insight_when_interval_includes_zero(con):
    # abwechselnd 1 und 3 Putts: Mittelwert um -0,05, Intervall schließt 0 ein
    seed_many(con, 10, putts_by_round=[1, 3] * 5)
    putting = next(i for i in _sg(con) if i.kind.startswith("sg_") and "putting" in i.id)
    assert putting.status == "unsicher" and "Unsicher" in putting.text
    visible = [i.id for i in generate(con, BASELINE, source="arccos")]
    assert "sg_loss:putting:arccos" not in visible  # standardmäßig unterdrückt
    shown = [i.id for i in generate(con, BASELINE, source="arccos", include_uncertain=True)]
    assert "sg_loss:putting:arccos" in shown


def test_strength_is_reported_when_clearly_positive(con):
    seed_many(con, 8)  # Abschläge landen weit besser als die Baseline erwartet
    strengths = [i for i in _sg(con) if i.kind == "sg_strength"]
    assert strengths and all(i.status == "belegt" and (i.effect or 0) > 0 for i in strengths)


def test_too_few_rounds_give_no_sg_insights(con):
    seed_many(con, 4, putts_by_round=[3] * 4)
    assert [i for i in _sg(con) if i.kind in ("sg_loss", "sg_strength")] == []


def test_low_coverage_only_yields_data_quality_hint(con):
    seed_many(con, 8)
    # Löcher unvollständig machen: Score höher als erfasste Schläge
    con.execute("update hole set strokes = strokes + 1")
    hints = _sg(con)
    assert [i.kind for i in hints] == ["data_quality"]
    assert "0% der Löcher" in hints[0].text and hints[0].status == "hinweis"


# --- Veränderung -------------------------------------------------------------------------------


def test_trend_insight_needs_clear_difference_and_enough_rounds(con):
    # 10 Runden mit 3 Putts, danach 10 mit 2: Putts pro Loch deutlich besser
    seed_many(con, 20, putts_by_round=[3] * 10 + [2] * 10)
    sg_obs = sg_round_observations(con, Filters(source="arccos"), BASELINE)
    found = {i.id: i for i in trend_insights(con, BASELINE, "arccos", sg_obs)}
    putts = found["trend:putts_per_hole:arccos"]
    assert putts.status == "belegt" and "wahrscheinlich besser" in putts.text
    assert putts.effect == pytest.approx(-18.0) and "-18,0" in putts.text
    assert putts.links["tool"] == "compare_periods"


def test_trend_needs_enough_rounds_in_both_groups(con):
    seed_many(con, 12, putts_by_round=[3] * 2 + [2] * 10)  # nur 2 Runden davor
    sg_obs = sg_round_observations(con, Filters(source="arccos"), BASELINE)
    assert [i for i in trend_insights(con, BASELINE, "arccos", sg_obs) if "putts" in i.id] == []


def test_no_trend_insight_without_difference(con):
    seed_many(con, 20, putts_by_round=[2, 3] * 10)
    sg_obs = sg_round_observations(con, Filters(source="arccos"), BASELINE)
    assert [i for i in trend_insights(con, BASELINE, "arccos", sg_obs)
            if "putts" in i.id] == []  # fmt: skip


# --- Range: Bias und Gapping ----------------------------------------------------------------------


def add_range_shots(con, club: str, carries: list[float], sides: list[float], source: str = "mevo"):
    sid = f"{source}|{club}"
    con.execute(
        "insert or ignore into session values (?,?,?,?,?, 'h', 'i')",
        [sid, source, club, "range", datetime(2026, 7, 1)],
    )
    for i, (carry, side) in enumerate(zip(carries, sides, strict=True)):
        con.execute(
            "insert into shot (shot_id, session_id, source, shot_key, club_raw, club_type, "
            "measurement_method, carry_m, carry_side_m, flags, content_hash, raw_hash, "
            "import_run_id) values (?,?,?,?,?,?, 'radar', ?, ?, [], 'h', 'r', 'i')",
            [f"{sid}|{i}", sid, source, f"k{i}", club, club, carry, side],
        )


def test_bias_insight_requires_size_n_and_clear_interval(con):
    add_range_shots(con, "iron_7", [120.0] * 20, [6.0 + (i % 3) for i in range(20)])  # rechts ~7 m
    add_range_shots(
        con, "iron_8", [110.0] * 20, [20.0, -10.0] * 10
    )  # Mittel 5 m, aber breit gestreut
    add_range_shots(con, "iron_9", [100.0] * 10, [8.0] * 10)  # zu wenige Schläge
    add_range_shots(con, "wedge_gap", [80.0] * 20, [0.5] * 20)  # kein Bias
    found = {i.id: i for i in bias_insights(con)}
    right = found["club_bias:iron_7:mevo"]
    assert right.status == "belegt" and right.effect is not None and right.effect > 6
    assert "rechts" in right.text and "n = 20 Schläge" in right.text
    assert found["club_bias:iron_8:mevo"].status == "unsicher"
    assert "club_bias:iron_9:mevo" not in found and "club_bias:wedge_gap:mevo" not in found


def test_gap_insight_for_overlapping_and_large_gaps(con):
    add_range_shots(con, "iron_7", [120.0, 121.0] * 10, [0.0] * 20)
    add_range_shots(con, "iron_8", [118.0, 119.0] * 10, [0.0] * 20)  # Lücke ~2 m: Überlappung
    add_range_shots(con, "iron_9", [95.0, 96.0] * 10, [0.0] * 20)  # Lücke ~23 m: zu groß
    found = {i.id: i for i in gap_insights(con)}
    overlap = found["gap:iron_7:iron_8:mevo:radar"]
    assert overlap.status == "belegt" and "überlappen" in overlap.text and overlap.unit == "m"
    large = found["gap:iron_8:iron_9:mevo:radar"]
    assert large.status == "belegt" and "große Lücke" in large.text


# --- Zusammenführung, Determinismus, Zugänge ------------------------------------------------------


def test_never_mixes_sources_and_orders_by_importance(con):
    seed_many(con, 8, putts_by_round=[3] * 8, source="arccos")
    seed_many(con, 8, putts_by_round=[3] * 8, source="garmin_platform")
    found = generate(con, BASELINE)
    assert {i.source for i in found} == {"arccos", "garmin_platform"}
    assert all(i.id.endswith(i.source or "") for i in found if i.kind.startswith("sg_"))
    losses = [i for i in found if i.kind == "sg_loss"]
    assert [i.priority for i in found] == sorted((i.priority for i in found), reverse=True)
    assert (
        losses[0].effect is not None
        and losses[0].effect <= min(i.effect or 0 for i in losses) + 1e-9
    )  # größter Verlust zuerst
    only = generate(con, BASELINE, source="garmin_platform")
    assert {i.source for i in only} == {"garmin_platform"}
    assert len(generate(con, BASELINE, limit=2)) == 2


def test_generation_is_deterministic(con):
    seed_many(con, 10, putts_by_round=[3, 2] * 5)
    first = [(i.id, i.text, i.effect) for i in generate(con, BASELINE, include_uncertain=True)]
    second = [(i.id, i.text, i.effect) for i in generate(con, BASELINE, include_uncertain=True)]
    assert first == second and first


def test_empty_database_has_no_insights(con):
    assert generate(con, BASELINE) == []


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    data = tmp_path / "d"
    c = open_db(data / "shotlake.duckdb")
    seed_many(c, 8, putts_by_round=[3] * 8)
    c.close()
    return Workspace(data, tmp_path / "club_map.toml", BASELINE_PATH)


def structured(server: Any, args: dict[str, Any]) -> Any:
    result: Any = asyncio.run(server.call_tool("get_insights", args))
    return result.structured_content


def test_mcp_get_insights(ws: Workspace):
    server = create_server(ws, read_only=True)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_insights" in names
    r: dict[str, Any] = structured(server, {})
    assert r["baseline"]["id"] == "hcp18w-approx-v1" and r["include_uncertain"] is False
    # über die Dedup-Sicht ohne Quellenfilter: ein gemeinsamer Bereich ("alle")
    putting = next(i for i in r["insights"] if i["id"] == "sg_loss:putting:alle")
    assert (
        putting["ci"]["high"] < 0 and putting["n"] == 8 and putting["links"]["tool"] == "get_stats"
    )
    limited = structured(server, {"limit": 1})
    assert len(limited["insights"]) == 1
    with pytest.raises(Exception, match="limit"):
        asyncio.run(server.call_tool("get_insights", {"limit": 0}))


def test_cli_insights(ws: Workspace, capsys: pytest.CaptureFixture[str]):
    assert main(["--data-dir", str(ws.data_dir), "insights"]) == 0
    out = capsys.readouterr().out
    assert "[belegt] Putten kostet" in out and "Baseline hcp18w-approx-v1" in out
    assert main(["--data-dir", str(ws.data_dir), "insights", "--source", "nix"]) == 0
    assert "Keine belegten Hinweise" in capsys.readouterr().out


def test_web_insights_page(ws: Workspace):
    client = TestClient(create_app(ws.data_dir, ws.club_map, BASELINE_PATH))
    html = client.get("/insights").text
    assert "Putten kostet ca. 18,9 Schläge" in html and "belegt" in html
    assert "Nachprüfen per MCP" in html and "Hinweise" in html
    assert "Keine belegten Hinweise" in client.get("/insights?source=garmin_platform").text
    assert client.get("/insights?uncertain=1").status_code == 200
