"""Trackman-Connector: Parser, Cursor und Live-Client gegen Fakes (kein Netz)."""

import base64
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from shotlake.connectors import trackman_queries as q
from shotlake.connectors.api import AuthError, RawRecord
from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.http import Response
from shotlake.connectors.trackman import (
    TrackmanConnector,
    parse_round,
    parse_session,
    trackman_club,
)
from shotlake.connectors.trackman_live import TrackmanClient, load_token

SESSION = {
    "_kind": "session", "id": "S1", "kind": "RANGE_PRACTICE", "time": "2026-11-01T10:00:00Z",
    "strokes": [
        {"club": "IRON7", "rangeTime": "2026-11-01T10:01:00Z", "measurement": {
            "ballSpeed": 40.0, "carry": 120.5, "total": 125.0, "carrySide": 3.0,
            "launchAngle": 18.0, "ballSpin": 6000.0}},
        {"club": "WEDGE52", "rangeTime": None, "measurement": {"carry": 80.0, "spinRate": 7000.0}},
    ],
}  # fmt: skip


def card(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_kind": "round", "id": "C1", "startedAt": "2026-11-02T09:00:00Z", "isCompleted": True,
        "par": 36, "teeName": "Rot", "course": {"displayName": "Sim Links"},
        "tees": [{"name": "Rot", "gender": "FEMALE", "rating": 70.1, "slope": 120}],
        "holes": [{
            "holeNumber": 1, "par": 4, "strokeIndex": 5, "grossScore": 5, "putts": 2,
            "isPlayed": True, "pinPosition": None,
            "shots": [
                {"shotNumber": 1, "club": "DRIVER", "total": 180.0, "launchLie": "Tee",
                 "finalLie": "Fairway", "launchPosition": None, "finalPosition": None},
                {"shotNumber": 2, "club": "IRON7", "total": 100.0, "launchLie": "Fairway",
                 "finalLie": "Moon", "launchPosition": None, "finalPosition": None},
            ],
        }],
    }  # fmt: skip
    return base | over


def test_club_names() -> None:
    assert trackman_club("IRON7") == "iron_7"
    assert trackman_club("WOOD3") == "wood_3"
    assert trackman_club("PITCHING_WEDGE") == "wedge_pitching"
    assert trackman_club("DRIVER") == "driver"
    assert trackman_club("WEDGE52") is None  # nicht geraten


def test_session_parsed_with_units_and_flags() -> None:
    s = parse_session(SESSION)
    assert s.session_id == "trackman:S1" and len(s.shots) == 2
    first, second = s.shots
    assert first.club_type == "iron_7" and first.carry_m == 120.5 and first.spin_rpm == 6000.0
    assert first.shot_time_utc == datetime(2026, 11, 1, 10, 1, tzinfo=UTC)
    assert second.club_type is None and second.flags == ("club_unmapped",)
    assert second.shot_time_utc is None  # fehlende Schlagzeit bleibt NULL
    assert second.spin_rpm == 7000.0 and second.ball_speed_ms is None
    assert any("WEDGE52" in w for w in s.warnings)


def test_session_without_strokes_is_drift() -> None:
    with pytest.raises(SchemaDriftError):
        parse_session({"id": "x", "time": "2026-11-01T10:00:00Z"})


def test_round_is_simulated_with_tee_and_warnings() -> None:
    r = parse_round(card())
    assert r.simulated and r.round_id == "trackman|C1" and r.tee_name == "Rot"
    assert r.course.tees[0].handicap_type == "WOMEN" and r.course.tees[0].rating == 70.1
    assert r.holes[0].strokes == 5 and r.holes[0].putts == 2
    assert [s.start_lie for s in r.shots] == ["tee", "fairway"]
    assert r.shots[1].end_lie == "other"
    assert any("Moon" in w for w in r.warnings)
    assert any("Geokoordinaten" in w for w in r.warnings)


def test_incomplete_round_is_in_progress() -> None:
    assert parse_round(card(isCompleted=False)).in_progress


class FakeClient:
    def __init__(self, acts: list[dict[str, Any]], cards: list[dict[str, Any]]) -> None:
        self.acts, self.cards = acts, cards
        self.since: list[datetime | None] = []

    def list_activities(self, since: datetime | None) -> list[dict[str, Any]]:
        self.since.append(since)
        return self.acts

    def session_detail(self, activity_id: str) -> dict[str, Any] | None:
        return None if activity_id == "none" else {k: SESSION[k] for k in ("time", "strokes")}

    def scorecards(self) -> list[dict[str, Any]]:
        return self.cards


def test_fetch_builds_records_and_cursor() -> None:
    acts = [
        {"id": "S1", "time": "2026-11-01T10:00:00Z", "kind": "RANGE_PRACTICE"},
        {"id": "none", "time": "2026-11-05T10:00:00Z", "kind": "SESSION"},
    ]
    client = FakeClient(acts, [{k: v for k, v in card().items() if k != "_kind"}])
    conn = TrackmanConnector(client=client)
    (page,) = list(conn.fetch(None))
    kinds = sorted(r.kind for r in page.records)
    assert kinds == ["round", "session"]
    cursor = json.loads(page.next_cursor or "")
    assert cursor["range"].startswith("2026-11-01") and cursor["round"].startswith("2026-11-02")
    assert any("ohne Schlagdaten" in n for n in conn.notes)
    list(conn.fetch(page.next_cursor))
    assert client.since[1] == datetime(2026, 10, 31, 10, 0, tzinfo=UTC)  # 1 Tag Überlapp


def test_open_round_holds_round_cursor_back() -> None:
    done = {k: v for k, v in card().items() if k != "_kind"}
    open_ = {**done, "id": "C2", "startedAt": "2026-11-03T09:00:00Z", "isCompleted": False}
    (page,) = list(TrackmanConnector(client=FakeClient([], [done, open_])).fetch(None))
    assert json.loads(page.next_cursor or "")["round"] < "2026-11-03T09:00:00"


def test_contract_check_flags_missing_keys() -> None:
    conn = TrackmanConnector()
    assert not conn.contract_check(RawRecord("x", {"_kind": "session", "id": "x"})).ok
    assert not conn.contract_check(RawRecord("x", {"_kind": "other", "id": "x"})).ok
    assert conn.contract_check(RawRecord("S1", dict(SESSION))).ok


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_token_expired_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("GOLF_COACH_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("SHOTLAKE_TRACKMAN_TOKEN", raising=False)
    with pytest.raises(AuthError, match="Kein Trackman-Token"):
        load_token()
    (tmp_path / "token.json").write_text(json.dumps({"access_token": jwt(time.time() - 10)}))
    with pytest.raises(AuthError, match="abgelaufen"):
        load_token()
    fresh = jwt(time.time() + 3600)
    monkeypatch.setenv("SHOTLAKE_TRACKMAN_TOKEN", f"Bearer {fresh}")
    assert load_token() == fresh


class FakeTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses, self.calls = responses, []  # type: ignore[var-annotated]

    def get(self, url: str, headers: Any = None) -> Response:
        raise AssertionError

    def post_json(self, url: str, payload: dict[str, Any], headers: Any = None) -> Response:
        self.calls.append((payload, dict(headers or {})))
        return self.responses.pop(0)


def ok(data: dict[str, Any]) -> Response:
    return Response(200, json.dumps({"data": data}))


def test_client_paging_and_no_token_in_repr() -> None:
    page1 = {"me": {"activities": {"totalCount": 3, "items": [{"id": "a"}, {"id": "b"}]}}}
    page2 = {"me": {"activities": {"totalCount": 3, "items": [{"id": "c"}]}}}
    tr = FakeTransport([ok(page1), ok(page2)])
    client = TrackmanClient(token="SECRET", transport=tr)
    client_page = 2
    import shotlake.connectors.trackman_live as live

    live.PAGE_SIZE, old = client_page, live.PAGE_SIZE
    try:
        items = client.list_activities(None)
    finally:
        live.PAGE_SIZE = old
    assert [i["id"] for i in items] == ["a", "b", "c"]
    assert tr.calls[0][1]["Authorization"] == "Bearer SECRET"
    assert tr.calls[0][0]["variables"]["kinds"] == list(q.RANGE_KINDS)
    assert "SECRET" not in repr(client)


def test_client_errors() -> None:
    def run(resp: Response) -> None:
        TrackmanClient(token="T", transport=FakeTransport([resp])).check()

    with pytest.raises(AuthError):
        run(Response(401, ""))
    gql = {"errors": [{"message": "The current user is not authorized"}]}
    with pytest.raises(AuthError):
        run(Response(200, json.dumps(gql)))
    with pytest.raises(SchemaDriftError):
        run(Response(200, json.dumps({"errors": [{"message": "Fields `time` conflict"}]})))
    with pytest.raises(SchemaDriftError):
        run(Response(200, "kein json"))
    run(ok({"me": {"profile": {"id": "1"}}}))


def test_queries_use_distinct_time_aliases() -> None:
    assert "rangeTime: time" in q.SESSION_DETAIL and "strokeTime: time" in q.SESSION_DETAIL
    assert " time kind strokes" in q.SESSION_DETAIL


def test_lie_vocabulary_from_live_run() -> None:
    from shotlake.connectors.trackman import _lie

    unknown: set[str] = set()
    got = [_lie(v, unknown) for v in ("Deeprough", "Semirough", "HoleCup", "Fringe", "None", None)]
    assert got == ["rough", "rough", "hole", "other", None, None] and not unknown


def test_club_map_fills_unmapped_range_shots(tmp_path: Any) -> None:
    from shotlake.config import sync_club_map
    from shotlake.db import open_db
    from shotlake.ingest import ImportReport, _write

    con = open_db(tmp_path / "t.duckdb")
    rep = ImportReport(run_id="r", source="trackman", filename="x")
    _write(con, parse_session(dict(SESSION)), "h", rep)
    path = tmp_path / "club_map.toml"
    path.write_text('[trackman]\n52Wedge = "wedge_gap"\n'.replace("52Wedge", '"WEDGE52"'))
    sync_club_map(con, path)
    row = con.execute("select club_type, flags from shot where club_raw = 'WEDGE52'").fetchone()
    assert row == ("wedge_gap", [])


def test_overrides_fill_only_untyped_shots(tmp_path: Any) -> None:
    from shotlake.config import apply_club_overrides
    from shotlake.db import open_db
    from shotlake.ingest import ImportReport, _write

    con = open_db(tmp_path / "t.duckdb")
    data = dict(SESSION)
    data["strokes"] = [
        {"club": "", "rangeTime": "2026-11-01T10:01:00Z", "measurement": {"carry": 50.0}},
        {"club": "", "rangeTime": "2026-11-01T10:09:00Z", "measurement": {"carry": 60.0}},
        {"club": "IRON7", "rangeTime": "2026-11-01T10:02:00Z", "measurement": {"carry": 70.0}},
    ]
    _write(con, parse_session(data), "h", ImportReport(run_id="r", source="trackman", filename="x"))
    path = tmp_path / "o.toml"
    path.write_text(
        '[[override]]\nsource = "trackman"\nsession = "S1"\nclub = "wedge_pitching"\n'
        'from = "2026-11-01T10:00:00"\nto = "2026-11-01T10:05:00"\n'
    )
    assert apply_club_overrides(con, path) == 1
    rows = con.execute("select club_type from shot order by carry_m").fetchall()
    assert rows == [("wedge_pitching",), (None,), ("iron_7",)]


def test_simulator_sessions_use_method_sim() -> None:
    sim = parse_session({**SESSION, "kind": "SIMULATOR"})
    assert {s.measurement_method for s in sim.shots} == {"sim"} and sim.shots[0].indoor
    assert {s.measurement_method for s in parse_session(SESSION).shots} == {"radar"}
