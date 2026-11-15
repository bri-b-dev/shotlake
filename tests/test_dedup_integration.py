"""Dedup in Auswertungen, CLI, MCP und Weboberfläche (synthetische Runden in zwei Quellen)."""

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotlake import dedup
from shotlake.analytics.course_shots import club_distances
from shotlake.analytics.metrics import Context, Filters, observations
from shotlake.analytics.score import round_scores
from shotlake.analytics.sg import strokes_gained
from shotlake.baseline import load_baseline
from shotlake.cli import main
from shotlake.db import open_db
from shotlake.dedup import DedupConfig, Scope
from shotlake.mcp_server import create_server
from shotlake.web.app import create_app
from shotlake.workspace import Workspace
from tests.test_insights import add_round

BASELINE_PATH = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"
BASELINE = load_baseline(BASELINE_PATH)
CFG = DedupConfig()


def fill_gross(con) -> None:
    """Brutto-Summe je Runde setzen (die Importe tun das; der Testhelfer nicht)."""
    con.execute(
        "update round set gross_strokes = (select sum(strokes) from hole "
        "where hole.round_id = round.round_id)"
    )


def seed_pairs(con, n: int = 6, *, putts_b: int | None = None):
    """n Runden, jede in `arccos` (führend für Schläge) und `bebrassie` (führend für Score)."""
    for i in range(n):
        when = datetime(2026, 6, 1) + timedelta(days=i)
        add_round(con, i, when, putts=2, source="arccos")
        add_round(con, i, when + timedelta(minutes=10), putts=putts_b or 2, source="bebrassie")
    fill_gross(con)
    return dedup.run(con, CFG)


@pytest.fixture
def con(tmp_path: Path):
    c = open_db(tmp_path / "t.duckdb")
    yield c
    c.close()


def test_pairs_are_linked_and_leaders_are_split_by_field_group(con):
    report = seed_pairs(con)
    assert report.auto == 6 and report.proposed == 0
    scope = Scope.build(con, CFG)
    # Score: bebrassie führt, Schläge: arccos führt (Prioritäten aus der Konfiguration)
    assert scope.exclude("score") == {f"arccos|r{i}" for i in range(6)}
    assert scope.exclude("shots") == {f"bebrassie|r{i}" for i in range(6)}


def test_round_scores_drop_duplicates_only_when_asked(con):
    seed_pairs(con)
    scope = Scope.build(con, CFG)
    assert len(round_scores(con)) == 12  # ohne Scope: alles, wie bisher
    kept = round_scores(con, exclude=scope.exclude("score"))
    assert len(kept) == 6 and {s.round_id.split("|")[0] for s in kept} == {"bebrassie"}
    assert len(round_scores(con, source="arccos")) == 6  # Quellenfilter bleibt unberührt


def test_rating_is_borrowed_from_linked_round_and_marked(con):
    seed_pairs(con, 2)
    con.execute("update round set tee_rating = 72.0, tee_slope = 130 where source = 'arccos'")
    scope = Scope.build(con, CFG)
    bebrassie = round_scores(con, source="bebrassie", donors=scope.donors)
    assert all(s.rating_source == "arccos" for s in bebrassie)
    assert round_scores(con, source="bebrassie")[0].rating_source is None  # ohne Donor nichts


def test_strokes_gained_and_course_shots_exclude_duplicates(con):
    seed_pairs(con)
    scope = Scope.build(con, CFG)
    both = strokes_gained(con, BASELINE)
    deduped = strokes_gained(con, BASELINE, exclude_rounds=scope.exclude("shots"))
    assert len(both.holes) == 2 * len(deduped.holes) and len(deduped.holes) == 36
    assert {h.round_id.split("|")[0] for h in deduped.holes} == {"arccos"}
    n_all = sum(c.n for c in club_distances(con, min_n=1))
    n_dedup = sum(c.n for c in club_distances(con, min_n=1, exclude_rounds=scope.exclude("shots")))
    assert n_all == 2 * n_dedup


def test_metrics_use_the_right_leader_per_group(con):
    seed_pairs(con)
    ctx = Context(Scope.build(con, CFG))
    putts = observations(con, "putts_per_hole", Filters(), BASELINE, ctx)
    assert len(putts) == 6 and {o.source for o in putts} == {"arccos"}  # Lochdetails: shots-Gruppe
    gross = observations(con, "gross", Filters(), BASELINE, ctx)
    assert len(gross) == 6 and {o.source for o in gross} == {"bebrassie"}  # Score: score-Gruppe
    sg = observations(con, "sg_total_per_hole", Filters(), BASELINE, ctx)
    assert len(sg) == 6
    assert len(observations(con, "gross", Filters(), BASELINE, Context())) == 12  # ohne Scope
    # Quellenfilter: keine Ausschlüsse
    assert len(observations(con, "gross", Filters(source="arccos"), BASELINE, ctx)) == 6


# --- CLI ---


def test_cli_dedup_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    add_round(con, 0, datetime(2026, 6, 1), source="arccos")
    add_round(con, 0, datetime(2026, 6, 1, 0, 5), putts=3, source="bebrassie")  # Score weicht ab
    fill_gross(con)
    con.close()
    args = ["--data-dir", str(data), "--dedup-config", str(tmp_path / "keine.toml")]
    assert main([*args, "dedup"]) == 0
    out = capsys.readouterr().out
    assert "vorgeschlagen 1" in out and "Offene Vorschläge: 1" in out
    assert main([*args, "dedup", "accept-all"]) == 0
    assert "1 Vorschläge bestätigt" in capsys.readouterr().out
    assert main([*args, "dedup", "list"]) == 0
    assert "Verknüpfte Runden: 1" in capsys.readouterr().out
    assert main([*args, "dedup", "reject", "arccos|r0", "bebrassie|r0"]) == 0
    assert main([*args, "dedup"]) == 0  # Neuberechnung ändert die Entscheidung nicht
    out = capsys.readouterr().out
    assert "Verknüpfte Runden: 0" in out and "Offene Vorschläge: 0" in out
    assert main([*args, "dedup", "confirm", "gibtsnicht", "arccos|r0"]) == 2
    assert "Fehler" in capsys.readouterr().err


def test_cli_analyses_dedup_by_default_and_can_include_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed_pairs(con)
    con.close()
    args = ["--data-dir", str(data), "--dedup-config", str(tmp_path / "keine.toml")]
    assert main([*args, "dedup"]) == 0
    capsys.readouterr()
    assert main([*args, "sg"]) == 0
    assert "Vollständig erfasste Löcher: 36 von 36" in capsys.readouterr().out
    assert main([*args, "sg", "--include-duplicates"]) == 0
    assert "Vollständig erfasste Löcher: 72 von 72" in capsys.readouterr().out
    assert main([*args, "sg", "--source", "bebrassie"]) == 0
    assert "36 von 36" in capsys.readouterr().out
    assert main([*args, "rounds"]) == 0
    assert capsys.readouterr().out.count("2026-06-") == 6
    assert main([*args, "rounds", "--include-duplicates"]) == 0
    assert capsys.readouterr().out.count("2026-06-") == 12


# --- MCP ---


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    seed_pairs(con)
    con.close()
    return Workspace(data, tmp_path / "c.toml", BASELINE_PATH, tmp_path / "keine.toml")


def call(server: Any, name: str, **args: Any) -> Any:
    result: Any = asyncio.run(server.call_tool(name, args))
    return result.structured_content


def test_mcp_stats_are_deduplicated_unless_asked_otherwise(ws: Workspace):
    server = create_server(ws, read_only=True)
    deduped = call(server, "get_stats", metric="gross")
    assert deduped["n"] == 6 and deduped["deduplicated"] is True
    assert not any(w.startswith("mixed_sources") for w in deduped["warnings"])
    allr = call(server, "get_stats", metric="gross", include_duplicates=True)
    assert allr["n"] == 12 and allr["deduplicated"] is False
    assert any(w.startswith("mixed_sources") for w in allr["warnings"])
    one = call(server, "get_stats", metric="gross", source="arccos")
    assert one["n"] == 6 and one["deduplicated"] is False
    sg = call(server, "get_strokes_gained")
    assert sg["coverage"]["holes"] == 36
    ins = call(server, "get_insights", include_uncertain=True)
    assert all("alle" in i["id"] or i["kind"] in ("club_bias", "gap") for i in ins["insights"])


def test_mcp_sync_source_runs_dedup(ws: Workspace):
    from tests.test_api_connector import doc, make

    server = create_server(ws, connector_factory=lambda _s: make([[doc(1)]]))
    result: Any = asyncio.run(server.call_tool("sync_source", {"source": "garmin_platform"}))
    assert result.structured_content["status"] == "ok"
    with ws.session() as con:
        assert con.execute("select count(*) from round_match").fetchone() == (6,)  # Abgleich lief


# --- Weboberfläche ---


@pytest.fixture
def client(ws: Workspace) -> TestClient:
    return TestClient(create_app(ws.data_dir, ws.club_map, BASELINE_PATH, ws.dedup_config))


def row_links(html: str, source: str) -> int:
    """Anzahl der Rundenzeilen einer Quelle (je Zeile genau ein Datums-Link)."""
    return len(re.findall(rf'<td><a href="[^"]*/rounds/{source}%7Cr\d', html))


def test_dashboard_and_rounds_are_deduplicated_by_default(client: TestClient):
    html = client.get("/").text
    assert "Alle Quellen, dedupliziert" in html and "n = 0 von 0" not in html
    rounds = client.get("/rounds").text
    assert row_links(rounds, "bebrassie") == 6 and row_links(rounds, "arccos") == 0
    assert "arccos, bebrassie" in rounds  # Spalte Quellen
    everything = client.get("/rounds?dups=1").text
    assert row_links(everything, "arccos") == 6 and row_links(everything, "bebrassie") == 6
    only = client.get("/rounds?source=arccos").text
    assert row_links(only, "arccos") == 6 and row_links(only, "bebrassie") == 0


def test_dedup_page_shows_links_and_proposals_and_actions_work(ws: Workspace, client: TestClient):
    html = client.get("/dedup").text
    assert "Verknüpfte Runden (6)" in html and "Vorschläge (0)" in html
    # eine Runde trennen: danach Vorschlag? nein, abgelehnt und nie wieder automatisch
    r = client.post(
        "/dedup/reject", data={"a": "arccos|r0", "b": "bebrassie|r0"}, follow_redirects=False
    )
    assert r.status_code == 303
    page = client.get("/dedup").text
    assert "Verknüpfte Runden (5)" in page and "Als verschieden festgelegt (1)" in page
    client.post("/dedup/run")
    assert "Verknüpfte Runden (5)" in client.get("/dedup").text  # bleibt getrennt
    client.post("/dedup/confirm", data={"a": "arccos|r0", "b": "bebrassie|r0"})
    assert "Verknüpfte Runden (6)" in client.get("/dedup").text
    bad = client.post(
        "/dedup/confirm", data={"a": "arccos|r0", "b": "arccos|r1"}, follow_redirects=True
    )
    assert "Fehler" in bad.text  # zwei Runden derselben Quelle


def test_proposals_banner_and_accept_all(tmp_path: Path):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    for i in range(3):
        when = datetime(2026, 6, 1) + timedelta(days=i)
        add_round(con, i, when, putts=2, source="arccos")
        add_round(con, i, when, putts=3, source="bebrassie")  # Score weicht ab: Vorschlag
    fill_gross(con)
    dedup.run(con, CFG)
    con.close()
    client = TestClient(create_app(data, tmp_path / "c.toml", BASELINE_PATH, tmp_path / "x.toml"))
    assert "mögliche Dubletten warten" in client.get("/").text
    assert "3 mögliche Dubletten offen" in client.get("/rounds").text
    page = client.get("/dedup").text
    assert "Vorschläge (3)" in page and "Score weicht ab" in page and "gleiche Runde" in page
    done = client.post("/dedup/accept-all", follow_redirects=True)
    assert "3 Vorschläge bestätigt" in done.text
    assert "mögliche Dubletten warten" not in client.get("/").text


def test_round_page_shows_dedup_status_and_leaders(client: TestClient):
    html = client.get("/rounds/bebrassie%7Cr1").text
    assert "Dubletten und führende Quelle" in html
    assert "verknüpft" in html and "Führend: Score" in html and "bebrassie" in html
    assert 'href="/rounds/arccos%7Cr1' in html


def test_manual_merge_form_links_rounds_that_the_rules_do_not(tmp_path: Path):
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    add_round(con, 0, datetime(2026, 6, 1, 9, 0), source="arccos")
    add_round(con, 0, datetime(2026, 6, 1, 18, 0), source="bebrassie")  # 9 h später
    fill_gross(con)
    dedup.run(con, CFG)
    con.close()
    client = TestClient(create_app(data, tmp_path / "c.toml", BASELINE_PATH, tmp_path / "x.toml"))
    assert "Verknüpfte Runden (0)" in client.get("/dedup").text
    client.post("/dedup/confirm", data={"a": "arccos|r0", "b": "bebrassie|r0"})
    assert "Verknüpfte Runden (1)" in client.get("/dedup").text


def test_simulated_rounds_never_match_real_ones(con) -> None:
    add_round(con, 1, datetime(2026, 6, 1, 10), putts=2, source="arccos")
    add_round(con, 1, datetime(2026, 6, 1, 10, 5), putts=2, source="trackman")
    con.execute("update round set simulated = true where source = 'trackman'")
    fill_gross(con)
    report = dedup.run(con, CFG)
    assert report.auto == 0 and not dedup.open_proposals(con)


def test_simulated_rounds_stay_out_of_combined_stats_and_sg(con) -> None:
    add_round(con, 1, datetime(2026, 6, 1, 10), putts=2, source="arccos")
    add_round(con, 2, datetime(2026, 6, 2, 10), putts=2, source="trackman")
    con.execute("update round set simulated = true where source = 'trackman'")
    fill_gross(con)
    ctx = Context()
    combined = ctx.rounds(con, Filters())
    assert {r.round_id.split("|")[0] for r in combined} == {"arccos"}
    assert {r.round_id.split("|")[0] for r in ctx.rounds(con, Filters(source="trackman"))} == {
        "trackman"
    }
    assert {h.round_id.split("|")[0] for h in strokes_gained(con, BASELINE).holes} <= {"arccos"}
    only_sim = strokes_gained(con, BASELINE, source="trackman").holes
    assert only_sim and all(h.round_id.startswith("trackman") for h in only_sim)
