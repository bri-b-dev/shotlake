"""BeBrassie-Parser und -Connector mit synthetischen Dokumenten (Form aus der Probe)."""

import json
from pathlib import Path
from typing import Any

import pytest

from shotlake.analytics.score import round_scores
from shotlake.analytics.sg import strokes_gained
from shotlake.baseline import load_baseline
from shotlake.connectors.api import AuthError
from shotlake.connectors.bebrassie import (
    VOLATILE_KEYS,
    BeBrassieConnector,
    parse_round,
)
from shotlake.connectors.errors import SchemaDriftError
from shotlake.db import open_db
from shotlake.ingest_api import get_cursor, sync_api

BASELINE = load_baseline(Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml")
LON, LAT = 13.0, 58.0  # GeoJSON-Reihenfolge: [Länge, Breite]
M_PER_DEG = 111_194.9


def pt(d_south_m: float = 0.0) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [LON, LAT - d_south_m / M_PER_DEG]}


def shot(hole_id: str, number: int, club: str, frm: str, to: str, d_from: float, d_to: float):
    return {
        "_id": f"s{hole_id}{number}", "courseHole": hole_id, "number": number,
        "calculatedDistance": abs(d_from - d_to),
        "club": {"_id": "c1", "clubName": club, "clubShortcut": club[:2], "clubType": "x"},
        "location": pt(d_from), "destination": pt(d_to), "remainingDistance": d_to,
        "shotFrom": frm, "shotTo": to, "shotType": "tee" if number == 1 else "approach",
    }  # fmt: skip


def doc(round_id: str = "r1", start: str = "2026-07-01T10:00:00.000Z", **over: Any):
    holes = [
        {"_id": "h1", "holeNumber": 1, "parMen": 4, "parWomen": 4, "handicapMan": 5,
         "handicapWomen": 7, "location": pt(0), "distanceToGreen": 300},
        {"_id": "h2", "holeNumber": 2, "parMen": 3, "parWomen": 3, "handicapMan": 11,
         "handicapWomen": 13, "location": pt(0), "distanceToGreen": 150},
    ]  # fmt: skip
    shots = [
        shot("h1", 1, "Driver", "Tee", "Fairway", 300, 100),
        shot("h1", 2, "Eisen 7", "Fairway", "Green", 100, 5),
        shot("h1", 3, "Putter", "Green", "Green", 5, 1),
        shot("h1", 4, "Putter", "Green", "Green", 1, 0),
        shot("h2", 1, "Eisen 8", "Tee", "Green", 150, 8),
        shot("h2", 2, "Putter", "Green", "Green", 8, 0),
    ]
    played = [
        {"isPickup": False, "numShots": 4, "holeIndex": 0, "courseHolePar": 4, "holeNumber": 1,
         "courseHole": "h1", "distanceToGreen": 300, "overall": {"score": 0}},
        {"isPickup": False, "numShots": 2, "holeIndex": 1, "courseHolePar": 3, "holeNumber": 2,
         "courseHole": "h2", "distanceToGreen": 150, "overall": {"score": -1}},
    ]  # fmt: skip
    base = {
        "_id": round_id, "startedAt": start, "finishedAt": "2026-07-01T12:00:00.000Z",
        "roundType": "social", "scoreOnly": False, "signedRound": True, "useForHci": True,
        "totalHoles": 2, "totalShots": 6, "proPlayerType": "WOMEN18",
        "strokesGained": {"WOMEN18": -1.2},
        "course": {"_id": "c9", "name": "Bahn 1", "golfClub": {
            "_id": "g", "name": "Testclub", "address": {"city": "Teststadt", "location": pt(0)}}},
        "courseTee": {"name": "Rot", "totalPar": 72, "ratingMen": 70.0, "ratingWomen": 74.3,
                      "slopeMen": 125, "slopeWomen": 129},
        "courseHoles": holes, "shots": shots, "playedHoles": played,
        "pinPositions": {"0": pt(0), "1": pt(0)},
        "hciAvg10": 40.0, "sgAvg10": -1.0, "userAgent": "Geheim/1.0", "note": "privat",
        "user": {"name": "Klarname"},
    }  # fmt: skip
    return {**base, **over}


def test_parse_round_maps_course_tee_holes_and_shots():
    r = parse_round(doc())
    assert r.round_id == "bebrassie|r1" and r.course.course_id == "bebrassie|c9"
    assert r.course.name == "Testclub" and r.course.city == "Teststadt" and r.course.par_total == 72
    assert r.course.lat == pytest.approx(LAT) and r.course.lon == pytest.approx(LON)  # lat/lon
    assert (r.tee_name, r.tee_rating, r.tee_slope) == ("Rot", 74.3, 129)  # Damenwerte
    assert {(t.handicap_type, t.rating) for t in r.course.tees} == {("WOMEN", 74.3), ("MEN", 70.0)}
    assert r.round_type == "social" and r.holes_played == 2 and r.gross_strokes == 6
    h1, h2 = r.holes
    assert (h1.par, h1.stroke_index, h1.strokes, h1.putts, h1.penalties) == (4, 7, 4, 2, None)
    assert (h2.par, h2.stroke_index, h2.putts) == (3, 13, 1)
    assert h1.pin_lat == pytest.approx(LAT) and h1.pin_lon == pytest.approx(LON)
    assert h1.fairway_outcome is None  # Seite des Fehlschlags nicht belegt
    assert len(r.shots) == 6
    first = r.shots[0]
    assert (first.start_lie, first.end_lie, first.shot_type) == ("tee", "fairway", "TEE")
    assert first.club_raw == "Driver" and first.club_type == "driver"
    assert r.shots[1].club_type == "iron_7" and r.shots[2].club_type == "putter"
    assert first.distance_m == pytest.approx(200.0) and first.shot_source == "BEBRASSIE"
    assert first.start_lat == pytest.approx(LAT - 300 / M_PER_DEG)
    assert r.warnings == []


def test_men_rating_when_player_group_is_men_and_none_when_unknown():
    men = parse_round(doc(proPlayerType="MEN18", strokesGained={"MEN18": 0.1}))
    assert (men.tee_rating, men.tee_slope) == (70.0, 125)
    assert men.holes[0].stroke_index == 5  # handicapMan
    unknown = parse_round(doc(proPlayerType=None, strokesGained={}))
    assert unknown.tee_rating is None and unknown.tee_slope is None
    assert unknown.holes[0].stroke_index is None
    assert any("Wertung" in w for w in unknown.warnings)


def test_unknown_values_are_warned_not_guessed():
    d = doc()
    d["shots"][0]["shotFrom"] = "Abschlagbox Plus"
    d["shots"][1]["shotTo"] = "Wasser"
    d["shots"][1]["shotType"] = "Strafe"
    r = parse_round(d)
    assert r.shots[0].start_lie == "other" and r.shots[1].end_lie == "other"
    assert r.shots[1].shot_type == "STRAFE"
    assert any("Abschlagbox Plus" in w and "Wasser" in w for w in r.warnings)


def test_putts_only_counted_when_shot_count_matches_score():
    d = doc()
    d["playedHoles"][0]["numShots"] = 5  # ein Schlag fehlt in `shots`
    r = parse_round(d)
    assert r.holes[0].strokes == 5 and r.holes[0].putts is None  # nicht geraten
    assert r.holes[1].putts == 1


def test_pickup_has_no_score_and_is_reported():
    d = doc()
    d["playedHoles"][1]["isPickup"] = True
    r = parse_round(d)
    assert r.holes[1].strokes is None and r.holes_played == 1 and r.gross_strokes == 4
    assert any("aufgenommene" in w for w in r.warnings)


def test_score_only_round_has_holes_but_no_shots():
    d = doc(scoreOnly=True, shots=[])
    r = parse_round(d)
    assert r.shots == [] and r.holes_played == 2 and r.holes[0].putts is None


def test_list_entry_without_detail_is_header_only():
    d = doc()
    for key in ("playedHoles", "courseHoles", "shots"):
        del d[key]
    r = parse_round(d)
    assert r.holes == [] and r.shots == [] and any("Listeneintrag" in w for w in r.warnings)


def test_empty_pin_coordinates_fall_back_to_green_location():
    d = doc(pinPositions={"0": {"type": "Point", "coordinates": []}})
    r = parse_round(d)
    assert r.holes[0].pin_lat == pytest.approx(LAT)  # Grünmitte des Lochs


def test_drift_on_missing_essentials():
    for key in ("_id", "startedAt", "course"):
        bad = doc()
        del bad[key]
        with pytest.raises(SchemaDriftError):
            parse_round(bad)


# --- Connector ---


class FakeClient:
    def __init__(self, items: list[dict[str, Any]], details: dict[str, dict[str, Any]]):
        self.items, self.details = items, details
        self.shown: list[str] = []

    def list_rounds(self) -> list[dict[str, Any]]:
        return self.items

    def show_round(self, round_id: str) -> dict[str, Any] | None:
        self.shown.append(round_id)
        return self.details.get(round_id)


def item(round_id: str, start: str) -> dict[str, Any]:
    return {
        "_id": round_id, "startedAt": start, "roundType": "social", "signedRound": True,
        "course": {"_id": "c9", "name": "Bahn 1", "courseData": {"huge": [1] * 100}},
        "user": "Klarname-Referenz", "hciAvg10": 41.0,
    }  # fmt: skip


def make_client(*rounds: tuple[str, str], missing: set[str] | None = None) -> FakeClient:
    items = [item(rid, start) for rid, start in rounds]
    details = {rid: doc(rid, start) for rid, start in rounds if rid not in (missing or set())}
    return FakeClient(items, details)


def connector(client: Any) -> BeBrassieConnector:
    return BeBrassieConnector(client=client, sleep=lambda _s: None)


def test_sync_end_to_end_cursor_and_incremental(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = make_client(("r2", "2026-07-02T10:00:00.000Z"), ("r1", "2026-07-01T10:00:00.000Z"))
    rep = sync_api(con, connector(client), tmp_path / "raw")
    assert rep.status == "ok" and rep.rounds_new == 2 and rep.course_shots_new == 12
    assert get_cursor(con, "bebrassie") == "2026-07-02T10:00:00+00:00"
    # zweiter Lauf: nur neuere Runden werden im Detail geholt
    client2 = make_client(
        ("r3", "2026-07-03T10:00:00.000Z"), ("r2", "2026-07-02T10:00:00.000Z"),
        ("r1", "2026-07-01T10:00:00.000Z"),
    )  # fmt: skip
    rep = sync_api(con, connector(client2), None)
    assert client2.shown == ["r3"] and rep.rounds_new == 1
    # Rohdaten ohne Geometrie, Nutzerdaten, Gerätekennung und Notiz
    raw = json.loads(next((tmp_path / "raw" / "bebrassie").glob("*.json")).read_text())
    assert "courseData" not in raw["course"] and "user" not in raw
    assert "userAgent" not in raw and "note" not in raw


def test_failed_detail_keeps_cursor_and_is_reported(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = make_client(("r2", "2026-07-02T10:00:00.000Z"), ("r1", "2026-07-01T10:00:00.000Z"),
                         missing={"r1"})  # fmt: skip
    rep = sync_api(con, connector(client), None)
    assert rep.status == "ok" and any("ohne Detaildaten" in w for w in rep.warnings)
    assert get_cursor(con, "bebrassie") is None  # nichts geht verloren: später erneut versuchen
    assert rep.rounds_new == 2  # Listeneintrag wird als Kopf-Runde gespeichert


def test_rolling_averages_do_not_change_the_raw_hash(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = make_client(("r1", "2026-07-01T10:00:00.000Z"))
    sync_api(con, connector(client), tmp_path / "raw")
    con.execute("delete from sync_state")
    client.details["r1"] = {**client.details["r1"], **dict.fromkeys(VOLATILE_KEYS, 99.0)}
    rep = sync_api(con, connector(client), tmp_path / "raw")
    assert rep.rounds_new == 0 and con.execute("select count(*) from raw_object").fetchone() == (1,)


def test_missing_credentials_report_auth_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for var in ("EMAIL", "PASSWORD"):
        monkeypatch.delenv(f"SHOTLAKE_BEBRASSIE_{var}", raising=False)
    monkeypatch.setenv("SHOTLAKE_SOPS_FILE", str(tmp_path / "nix.json"))
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, BeBrassieConnector(sleep=lambda _s: None), None)
    assert rep.status == "error" and "Zugangsdaten" in rep.errors[0]
    with pytest.raises(AuthError):
        BeBrassieConnector(sleep=lambda _s: None).authenticate()


def test_rounds_and_sg_work_with_bebrassie_data(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    sync_api(con, connector(make_client(("r1", "2026-07-01T10:00:00.000Z"))), None)
    (score,) = round_scores(con, source="bebrassie")
    assert (score.gross, score.par, score.putts) == (6, 7, 3)
    assert score.part is None  # 2 Löcher: weder 9 noch 18, kein Differential
    res = strokes_gained(con, BASELINE, source="bebrassie")
    assert len(res.holes) == 2 and all(h.reason is not None for h in res.holes if not h.complete)
