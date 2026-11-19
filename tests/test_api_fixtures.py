"""Anonymisierte Rohdokumente (BeBrassie, Trackman) gegen Parser, Contract-Check und Sync.

Die Fixtures stammen aus echten Rohdaten und wurden mit `scripts/anonymize_fixture.py` bearbeitet
(Kennungen, Orte, Zeiten, Adressen). Sie decken je Dokumentart einen Fall ab.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from shotlake.connectors.api import RawRecord
from shotlake.connectors.bebrassie import BeBrassieConnector
from shotlake.connectors.bebrassie import parse_round as parse_bebrassie
from shotlake.connectors.trackman import TrackmanConnector, parse_round, parse_session
from shotlake.db import open_db
from shotlake.ingest_api import sync_api

FIXTURES = Path(__file__).parent / "fixtures"
BEBRASSIE = FIXTURES / "bebrassie"
TRACKMAN = FIXTURES / "trackman"


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def all_fixtures() -> list[Path]:
    return sorted([*BEBRASSIE.glob("*.json"), *TRACKMAN.glob("*.json")])


# --- Datenschutz ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", all_fixtures(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixture_has_no_personal_markers(path: Path) -> None:
    text = path.read_text()
    assert "@" not in text, "E-Mail-Adresse im Fixture"
    assert not re.search(r"https?://", text), "Web-Adresse im Fixture"
    if "address" in text:
        doc = load(path)
        address = doc["course"]["golfClub"]["address"]
        assert address["street"] == "Musterweg 1" and address["city"] == "Musterstadt"
        assert address["url"] == "example.org"


# --- BeBrassie -----------------------------------------------------------------------------------


def test_bebrassie_score_only_round_gives_header_values_only() -> None:
    r = parse_bebrassie(load(BEBRASSIE / "score_only_round.json"))
    assert r.holes == [] and r.shots == []
    assert (r.holes_total, r.gross_total) == (9, 56)
    assert any("nur Listeneintrag" in w for w in r.warnings)


def test_bebrassie_partial_round_has_rating_and_putts_as_shots() -> None:
    r = parse_bebrassie(load(BEBRASSIE / "partial_five_holes.json"))
    assert len(r.holes) == 5 and len(r.shots) == 35
    assert (r.tee_rating, r.tee_slope) == (70.3, 136)
    putts = [s for s in r.shots if s.shot_type == "PUTT"]
    assert putts and all(s.start_lie == "green" for s in putts)
    assert all(h.strokes is not None and h.strokes >= 1 for h in r.holes)


def test_bebrassie_penalty_counts_on_hole_not_as_shot() -> None:
    r = parse_bebrassie(load(BEBRASSIE / "nine_holes_with_penalty.json"))
    assert [h.penalties for h in r.holes] == [0, 0, 0, 0, 0, 0, 0, 1, 0]
    assert all(s.shot_type != "PENALTY" for s in r.shots)
    assert len(r.holes) == 9 and r.warnings == [] and r.errors == []


def test_bebrassie_coordinates_are_lonlat_pairs_in_metric_range() -> None:
    r = parse_bebrassie(load(BEBRASSIE / "partial_five_holes.json"))
    located = [s for s in r.shots if s.start_lat is not None and s.end_lat is not None]
    assert located
    for s in located:
        lat, lon, dist = s.start_lat or 0.0, s.start_lon or 0.0, s.distance_m
        assert -90 <= lat <= 90 and -180 <= lon <= 180
        assert dist is None or 0 <= dist < 600


@pytest.mark.parametrize("path", sorted(BEBRASSIE.glob("*.json")), ids=lambda p: p.name)
def test_bebrassie_contract_and_sync(path: Path, tmp_path: Path) -> None:
    doc = load(path)
    conn = BeBrassieConnector(client=None)
    assert not conn.contract_check(RawRecord(str(doc["_id"]), doc, "round")).errors

    class Client:
        def list_rounds(self) -> list[dict[str, Any]]:
            return [doc]  # der Listeneintrag trägt Kopfwerte und `course`

        def show_round(self, round_id: str) -> dict[str, Any] | None:
            return None if doc.get("scoreOnly") else doc

    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, BeBrassieConnector(client=Client(), sleep=lambda _s: None), None)
    assert rep.errors == [], rep.errors
    assert rep.rounds_new == 1
    again = sync_api(con, BeBrassieConnector(client=Client(), sleep=lambda _s: None), None)
    assert again.rounds_new == 0  # Idempotenz: dasselbe Dokument ergibt keine neue Runde


# --- Trackman ------------------------------------------------------------------------------------


SESSION_FILES = {
    "session_session.json": ("SESSION", "radar", {"iron_7"}),
    "session_shot_analysis.json": ("SHOT_ANALYSIS", "radar", {"iron_9"}),
    "session_map_my_bag.json": ("MAP_MY_BAG", "radar", {"wedge_pitching"}),
    "session_virtual_range.json": ("VIRTUAL_RANGE", "radar", {"driver"}),
}


@pytest.mark.parametrize("name", sorted(SESSION_FILES))
def test_trackman_sessions_per_activity_kind(name: str) -> None:
    kind, method, clubs = SESSION_FILES[name]
    doc = load(TRACKMAN / name)
    assert doc["kind"] == kind
    s = parse_session(doc)
    assert s.errors == [] and s.shots
    assert {x.measurement_method for x in s.shots} == {method}
    assert {x.club_type for x in s.shots} == clubs
    for shot in s.shots:  # SI-Einheiten: m und m/s, nicht yd oder mph
        assert shot.carry_m is None or 0 < shot.carry_m < 450
        assert shot.ball_speed_ms is None or 5 < shot.ball_speed_ms < 100


def test_trackman_simulator_session_is_sim_and_reports_unmapped_wedge() -> None:
    s = parse_session(load(TRACKMAN / "session_simulator.json"))
    assert {x.measurement_method for x in s.shots} == {"sim"}
    assert any("60Wedge" in w for w in s.warnings)  # steht nur in config/club_map.toml
    assert {x.club_type for x in s.shots} >= {"driver", "wood_7"}


def test_trackman_complete_round_is_simulated_with_all_holes() -> None:
    r = parse_round(load(TRACKMAN / "scorecard_complete.json"))
    assert r.simulated and not r.in_progress
    assert len(r.holes) == 18 and len(r.shots) == 57
    # echte Daten schreiben `Female`, nicht `FEMALE`; Sim-Runden tragen kein Rating an der Runde
    (tee,) = r.course.tees
    assert (tee.name, tee.rating, tee.slope, tee.handicap_type) == ("Orange", 71.2, 128, "WOMEN")
    assert all(s.start_lie != "None" for s in r.shots)


def test_trackman_incomplete_round_is_in_progress() -> None:
    r = parse_round(load(TRACKMAN / "scorecard_incomplete.json"))
    assert r.simulated and r.in_progress
    assert len(r.holes) == 9 and len(r.shots) == 2


class TrackmanFakeClient:
    """Liefert die Fixtures so, wie die Live-Abfragen sie liefern würden."""

    def __init__(self) -> None:
        docs = [load(p) for p in sorted(TRACKMAN.glob("*.json"))]
        self.sessions = {d["id"]: d for d in docs if d["_kind"] == "session"}
        self.cards = [
            {k: v for k, v in d.items() if k != "_kind"} for d in docs if d["_kind"] == "round"
        ]

    def list_activities(self, since: datetime | None) -> list[dict[str, Any]]:
        return [{"id": i, "time": d["time"], "kind": d["kind"]} for i, d in self.sessions.items()]

    def session_detail(self, activity_id: str) -> dict[str, Any] | None:
        d = self.sessions[activity_id]
        return {"time": d["time"], "strokes": d["strokes"]}

    def scorecards(self) -> list[dict[str, Any]]:
        return self.cards


def test_trackman_sync_end_to_end_and_idempotent(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, TrackmanConnector(client=TrackmanFakeClient()), tmp_path / "raw")
    assert rep.errors == [], rep.errors
    assert rep.sessions_new == 5 and rep.rounds_new == 2
    assert rep.shots_new == 3 + 2 + 4 + 2 + 6  # Range-Schläge je Session
    assert rep.course_shots_new == 57 + 2
    row = con.execute(
        "select count(*) from round where simulated and source = 'trackman'"
    ).fetchone()
    assert row is not None and row[0] == 2
    again = sync_api(con, TrackmanConnector(client=TrackmanFakeClient()), tmp_path / "raw")
    assert again.sessions_new == 0 and again.rounds_new == 0 and again.shots_new == 0


def test_bebrassie_score_only_round_does_not_hold_back_the_cursor(tmp_path: Path) -> None:
    """Reine Score-Runden haben nie Details: keine Anfrage, keine Meldung, Cursor rückt vor."""
    from shotlake.ingest_api import get_cursor

    doc = load(BEBRASSIE / "score_only_round.json")

    class Client:
        def __init__(self) -> None:
            self.shown: list[str] = []

        def list_rounds(self) -> list[dict[str, Any]]:
            return [doc]

        def show_round(self, round_id: str) -> dict[str, Any] | None:
            self.shown.append(round_id)
            return None

    client = Client()
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, BeBrassieConnector(client=client, sleep=lambda _s: None), None)
    assert rep.rounds_new == 1 and client.shown == []
    assert not any("ohne Detaildaten" in w for w in rep.warnings)
    assert get_cursor(con, "bebrassie") is not None
