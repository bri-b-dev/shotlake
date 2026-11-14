"""Garmin-Platz-Connector mit synthetischen Rundendaten (Form aus der Phase-0-Probe)."""

from pathlib import Path
from typing import Any

import pytest

from shotlake.connectors.api import AuthError, TransientError
from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.garmin_platform import GarminPlatformConnector, parse_round
from shotlake.db import open_db
from shotlake.ingest import ImportReport
from shotlake.ingest_api import get_cursor, sync_api
from shotlake.ingest_round import write_round
from shotlake.model import HoleRecord

E7 = 10_000_000


def scalar(con: Any, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


def summary(sid: int, start: str, in_progress: bool = False) -> dict[str, Any]:
    return {"id": sid, "startTime": start, "roundInProgress": in_progress}


def details(sid: int, start: str) -> dict[str, Any]:
    holes: list[dict[str, Any]] = [
        {
            "number": 1,
            "strokes": 5,
            "putts": 2,
            "penalties": 0,
            "fairwayShotOutcome": "RIGHT",
            "pinPositionLat": 50 * E7,
            "pinPositionLon": 8 * E7,
        },
        {"number": 2, "strokes": 3, "putts": 1, "penalties": 1},
        *({"number": n} for n in range(3, 19)),
    ]
    tee = {
        "name": "Gelb",
        "handicapType": "MEN",
        "rating": 72.1,
        "slope": 133,
        "holeHandicaps": "051301151107090317180802061214161004",
    }
    card = {
        "id": sid,
        "courseGlobalId": 1,
        "courseSnapshotId": 2,
        "startTime": start,
        "endTime": start,
        "roundType": "FRONT_9",
        "teeBox": "Gelb",
        "handicapType": "MEN",
        "teeBoxRating": 72.1,
        "teeBoxSlope": 133,
        "inProgress": False,
        "excludeFromStats": False,
        "holes": holes,
    }
    snapshot = {
        "courseGlobalId": 1,
        "courseSnapshotId": 2,
        "name": "Testplatz",
        "country": "DE",
        "city": "Teststadt",
        "lat": 50 * E7,
        "lon": 8 * E7,
        "roundPar": 72,
        "holePars": "435454354435444335",
        "tees": [tee],
    }
    return {"scorecardDetails": [{"scorecard": card}], "courseSnapshots": [snapshot]}


def shot_data() -> dict[str, Any]:
    def loc(lat: int, lie: str) -> dict[str, Any]:
        return {"lat": lat * E7, "lon": 8 * E7, "lie": lie, "lieSource": "CARTOGRAPHY"}

    shots = [
        {
            "shotOrder": 1,
            "shotTime": 1_780_000_000_000,
            "clubId": 7,
            "meters": 180.5,
            "shotType": "TEE",
            "shotSource": "DEVICE_AUTO",
            "startLoc": loc(50, "TeeBox"),
            "endLoc": loc(51, "Bunker"),
        },
        {
            "shotOrder": 2,
            "clubId": 0,
            "meters": 12.0,
            "shotType": "CHIP",
            "startLoc": loc(51, "Bunker"),
            "endLoc": loc(52, "Moor"),
        },
    ]
    return {
        "holeShots": [{"holeNumber": 1, "shots": shots}],
        "clubDetails": [{"id": 7, "clubTypeId": 1}],
    }


def payload(sid: int = 100, start: str = "2026-10-23T13:37:13.000Z") -> dict[str, Any]:
    return {
        "summary": summary(sid, start),
        "details": details(sid, start),
        "shot_data": shot_data(),
    }


class FakeClient:
    def __init__(self, rounds: list[tuple[int, str]], in_progress: set[int] | None = None):
        self.rounds = rounds  # neueste zuerst
        self.in_progress = in_progress or set()
        self.fail_scorecard_once = False
        self.calls: list[str] = []

    def get_golf_summary(self, start: int, limit: int) -> dict[str, Any]:
        page = self.rounds[start : start + limit]
        return {
            "totalRows": len(self.rounds),
            "scorecardSummaries": [summary(i, t, i in self.in_progress) for i, t in page],
        }

    def get_golf_scorecard(self, scorecard_id: int) -> dict[str, Any]:
        self.calls.append(f"card{scorecard_id}")
        if self.fail_scorecard_once:
            self.fail_scorecard_once = False
            raise ConnectionError("netz weg")
        return details(scorecard_id, dict(self.rounds)[scorecard_id])

    def get_golf_shot_data(self, scorecard_id: int) -> dict[str, Any]:
        return shot_data()


def connector(client: FakeClient) -> GarminPlatformConnector:
    return GarminPlatformConnector(client=client, sleep=lambda _s: None)


def test_parse_round_maps_units_lies_and_holes():
    r = parse_round(payload())
    assert r.round_id == "garmin_platform|100"
    assert r.course.name == "Testplatz" and r.course.course_id == "garmin_platform|1-2"
    assert r.course.lat == pytest.approx(50.0)
    assert (r.tee_name, r.tee_rating, r.tee_slope) == ("Gelb", 72.1, 133)
    assert r.holes_played == 2 and r.gross_strokes == 8
    h1, h2 = r.holes
    assert (h1.par, h1.stroke_index, h1.fairway_outcome) == (4, 5, "right")
    assert h1.pin_lat == pytest.approx(50.0)
    assert (h2.par, h2.putts, h2.penalties) == (3, 1, 1)
    s1, s2 = r.shots
    assert s1.distance_m == 180.5 and s1.start_lie == "tee" and s1.end_lie == "sand"
    assert s1.shot_time_utc is not None and s1.shot_time_utc.year == 2026
    assert s1.club_raw == "garmin_clubtype_1" and s2.club_raw is None
    assert s2.end_lie == "other"
    assert any("Moor" in w for w in r.warnings)


def test_parse_round_drift_on_missing_structure():
    bad = payload()
    del bad["details"]["courseSnapshots"]
    with pytest.raises(SchemaDriftError):
        parse_round(bad)


def test_sync_writes_round_holes_shots_and_cursor(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = FakeClient([(2, "2026-10-24T10:00:00.000Z"), (1, "2026-10-23T10:00:00.000Z")])
    rep = sync_api(con, connector(client), tmp_path / "raw")
    assert rep.status == "ok" and rep.rounds_new == 2
    assert scalar(con, "select count(*) from round") == 2
    assert scalar(con, "select count(*) from hole") == 4
    assert scalar(con, "select count(*) from course_shot") == 4
    assert scalar(con, "select count(*) from course") == 1
    assert scalar(con, "select count(*) from course_tee") == 1
    assert get_cursor(con, "garmin_platform") == "2026-10-24T10:00:00+00:00"


def test_second_sync_fetches_only_newer_rounds(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    rounds = [(1, "2026-10-23T10:00:00.000Z")]
    sync_api(con, connector(FakeClient(rounds)), None)
    client = FakeClient([(2, "2026-10-24T10:00:00.000Z"), *rounds])
    rep = sync_api(con, connector(client), None)
    assert client.calls == ["card2"]
    assert rep.rounds_new == 1
    again = FakeClient([(2, "2026-10-24T10:00:00.000Z"), *rounds])
    rep = sync_api(con, connector(again), None)
    assert rep.rounds_new == 0 and rep.course_shots_new == 0


def test_in_progress_round_is_skipped_and_cursor_not_advanced(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = FakeClient([(2, "2026-10-24T10:00:00.000Z")], in_progress={2})
    rep = sync_api(con, connector(client), None)
    assert rep.rounds_new == 0 and client.calls == []
    assert get_cursor(con, "garmin_platform") is None


def test_transient_error_retries_whole_fetch_without_losing_rounds(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    client = FakeClient([(2, "2026-10-24T10:00:00.000Z"), (1, "2026-10-23T10:00:00.000Z")])
    client.fail_scorecard_once = True
    rep = sync_api(con, connector(client), None)
    assert rep.status == "ok" and rep.rounds_new == 2


def test_handicap_relevant_survives_update(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    sync_api(con, connector(FakeClient([(1, "2026-10-23T10:00:00.000Z")])), None)
    con.execute("update round set handicap_relevant = true")
    changed = parse_round(payload(1, "2026-10-23T10:00:00.000Z"))
    changed.holes[0] = HoleRecord(number=1, par=4, strokes=6)
    rep = ImportReport("r", "garmin_platform", "x")
    write_round(con, changed, "h2", rep)
    assert rep.rounds_changed == 1
    assert scalar(con, "select handicap_relevant from round") is True
    assert scalar(con, "select strokes from hole where hole_number = 1") == 6


def test_auth_failure_is_reported(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = GarminPlatformConnector(tokenstore=str(tmp_path / "none"), sleep=lambda _s: None)
    rep = sync_api(con, c, None)
    assert rep.status == "error"


def test_error_mapping():
    m = GarminPlatformConnector._map
    assert isinstance(m(ConnectionError("x")), TransientError)

    class GarminConnectAuthenticationError(Exception): ...

    assert isinstance(m(GarminConnectAuthenticationError("x")), AuthError)
