"""Zwei SG-Baselines (eigenes Niveau und Ziel 18): Tabellen, Auswahl in CLI, MCP und Oberfläche."""

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotlake.baseline import (
    LIES,
    default_baseline_path,
    list_baselines,
    load_baseline,
    resolve_baseline_path,
)
from shotlake.cli import main
from shotlake.db import open_db
from shotlake.mcp_server import create_server
from shotlake.web.app import create_app
from shotlake.workspace import Workspace
from tests.test_insights import seed_many

DIRECTORY = Path(__file__).parent.parent / "baselines"
OWN = DIRECTORY / "hcp40w-approx-v1.toml"
GOAL = DIRECTORY / "hcp18w-approx-v1.toml"
CLUB_MAP = Path(__file__).parent.parent / "config" / "club_map.toml"
DISTANCES = (5, 10, 20, 30, 50, 75, 100, 150, 200, 250, 300, 350)


def test_both_baselines_are_labelled_approximations() -> None:
    own, goal = load_baseline(OWN), load_baseline(GOAL)
    assert (own.id, own.target_handicap, own.status) == ("hcp40w-approx-v1", 40.0, "approximation")
    assert (goal.id, goal.target_handicap, goal.status) == (
        "hcp18w-approx-v1",
        18.0,
        "approximation",
    )
    assert own.title == "Mein Niveau (Handicap 40)" and goal.title == "Ziel Handicap 18"
    assert "NICHT gemessen" in own.description and "NICHT gemessen" in goal.description


@pytest.mark.parametrize("lie", ["fairway", "rough", "sand", "green"])
def test_weaker_level_expects_more_strokes_everywhere(lie: str) -> None:
    own, goal = load_baseline(OWN), load_baseline(GOAL)
    distances = (0.5, 1, 2, 3, 5, 8, 12, 20, 30) if lie == "green" else DISTANCES
    for d in distances:
        e40, e18 = own.expected(lie, d), goal.expected(lie, d)
        assert e40 is not None and e18 is not None and e40 > e18, (lie, d)


def test_weaker_level_expects_more_strokes_from_the_tee() -> None:
    own, goal = load_baseline(OWN), load_baseline(GOAL)
    for d in (90, 150, 220, 300, 400, 450):
        assert own.expected("tee", d) > goal.expected("tee", d)  # type: ignore[operator]


@pytest.mark.parametrize("path", [OWN, GOAL], ids=["hcp40", "hcp18"])
def test_tables_are_consistent_with_each_other(path: Path) -> None:
    b = load_baseline(path)
    assert set(LIES) <= set(b.tables)
    for d in (90, 120, 150, 180, 220, 260, 300, 350):
        # ein Abschlag ist nie schlechter als ein Schlag aus dem Fairway auf gleicher Distanz
        assert b.expected("tee", d) <= b.expected("fairway", d) + 0.01, d  # type: ignore[operator]
    for d in DISTANCES:
        assert b.expected("fairway", d) <= b.expected("rough", d)  # type: ignore[operator]
        if d >= 20:
            assert b.expected("rough", d) <= b.expected("sand", d) + 0.5  # type: ignore[operator]


def test_hcp40_gives_about_par_plus_two_and_a_half_per_hole() -> None:
    """Anker der Näherung: Par 3 bei 150 m, Par 4 bei 330 m, Par 5 bei 440 m."""
    b = load_baseline(OWN)
    over = [b.expected("tee", 150) - 3, b.expected("tee", 330) - 4, b.expected("tee", 440) - 5]  # type: ignore[operator]
    assert all(1.7 <= v <= 3.4 for v in over), over
    assert 2.0 <= sum(over) / 3 <= 3.0


def test_listing_resolving_and_default() -> None:
    assert [b.id for b in list_baselines(DIRECTORY)] == ["hcp40w-approx-v1", "hcp18w-approx-v1"]
    assert resolve_baseline_path("hcp18w-approx-v1", DIRECTORY) == GOAL  # Kennung
    assert resolve_baseline_path("hcp40w-approx-v1", DIRECTORY) == OWN
    assert resolve_baseline_path(GOAL, DIRECTORY) == GOAL  # Pfad bleibt Pfad
    with pytest.raises(ValueError, match="Unbekannte Baseline"):
        resolve_baseline_path("gibt-es-nicht", DIRECTORY)
    assert default_baseline_path().name == "hcp18w-approx-v1.toml"  # Zielniveau ist Standard


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "d"
    con = open_db(d / "shotlake.duckdb")
    seed_many(con, 8, putts_by_round=[3] * 8)
    con.close()
    return d


def test_workspace_offers_both_with_default_first(data: Path) -> None:
    ws = Workspace(data, CLUB_MAP, OWN)
    assert [b.id for b in ws.available_baselines()] == ["hcp40w-approx-v1", "hcp18w-approx-v1"]
    assert ws.find_baseline(None).id == "hcp40w-approx-v1"
    assert ws.find_baseline("hcp18w-approx-v1").id == "hcp18w-approx-v1"
    with pytest.raises(ValueError, match="hcp40w-approx-v1"):
        ws.find_baseline("x")


def numbers(html: str) -> list[str]:
    return re.findall(r"lede-num[^>]*>\s*([-+]?\d+\.\d+)", html)


def test_web_switch_changes_strokes_gained_and_keeps_choice_in_links(data: Path) -> None:
    client = TestClient(create_app(data, CLUB_MAP, OWN))
    own = client.get("/").text
    goal = client.get("/?b=hcp18w-approx-v1").text
    assert "Mein Niveau (Handicap 40)" in own and "Ziel Handicap 18" in own
    assert 'aria-current="true" title="hcp40w-approx-v1"' in own
    assert 'aria-current="true" title="hcp18w-approx-v1"' in goal
    assert numbers(own) and numbers(goal) and numbers(own) != numbers(goal)
    assert "hcp18w-approx-v1" in goal and "Vergleich: Ziel Handicap 18" in goal
    assert "b=hcp18w-approx-v1" in goal and "/rounds?" in goal  # Navigation behält die Wahl
    assert client.get("/?b=unbekannt").text == own  # unbekannt: Standard


def test_web_switch_on_insights_and_round_detail(data: Path) -> None:
    client = TestClient(create_app(data, CLUB_MAP, OWN))
    own = client.get("/insights").text
    goal = client.get("/insights?b=hcp18w-approx-v1").text
    assert "Baseline hcp40w-approx-v1" in own and "Baseline hcp18w-approx-v1" in goal
    assert own != goal
    link = re.search(r'href="(/rounds/[^"]+)"', client.get("/rounds").text)
    assert link is not None
    detail = client.get(link.group(1) + "?b=hcp18w-approx-v1").text
    assert "Baseline hcp18w-approx-v1" in detail and "Ziel Handicap 18" in detail


def structured(server: Any, tool: str, args: dict[str, Any]) -> Any:
    result: Any = asyncio.run(server.call_tool(tool, args))
    return result.structured_content


def test_mcp_tools_accept_baseline(data: Path) -> None:
    server = create_server(Workspace(data, CLUB_MAP, OWN), read_only=True)
    default = structured(server, "get_insights", {})
    goal = structured(server, "get_insights", {"baseline": "hcp18w-approx-v1"})
    assert default["baseline"]["id"] == "hcp40w-approx-v1"
    assert goal["baseline"]["id"] == "hcp18w-approx-v1"
    stats = structured(
        server, "get_stats", {"metric": "sg_total_per_hole", "baseline": "hcp18w-approx-v1"}
    )
    assert (
        stats["value"] != structured(server, "get_stats", {"metric": "sg_total_per_hole"})["value"]
    )
    sg = structured(server, "get_strokes_gained", {"baseline": "hcp18w-approx-v1"})
    assert sg["baseline"]["id"] == "hcp18w-approx-v1"


def test_cli_baselines_and_sg_by_id(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(data), "baselines"]) == 0
    out = capsys.readouterr().out
    assert "hcp40w-approx-v1" in out and "Standard" in out and "hcp18w-approx-v1" in out
    assert main(["--data-dir", str(data), "sg", "--baseline", "hcp40w-approx-v1"]) == 0
    assert "Baseline hcp40w-approx-v1" in capsys.readouterr().out
    assert main(["--data-dir", str(data), "sg"]) == 0
    assert "Baseline hcp18w-approx-v1" in capsys.readouterr().out


def test_trend_insight_is_coloured_by_better_or_worse_not_by_sign(data: Path) -> None:
    from shotlake.analytics.insights import Insight
    from shotlake.web.app import _improved

    def trend(text: str, effect: float) -> Insight:
        return Insight("t", "trend", "belegt", 1.0, text, effect, "x", None, 5, None, headline=text)

    assert _improved(trend("Putts sind wahrscheinlich besser als davor.", -3.9)) is True
    assert _improved(trend("Putts sind wahrscheinlich schlechter als davor.", +3.9)) is False
    loss = Insight("l", "sg_loss", "belegt", 1.0, "x", -1.0, "x", None, 5, None)
    assert _improved(loss) is None  # Verluste und Stärken: Vorzeichen entscheidet
