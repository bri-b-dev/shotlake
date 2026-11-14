"""Arccos: devalue-Dekoder, Rundenparser und HAR-Client (synthetische Daten, Form aus dem HAR)."""

import json
from pathlib import Path
from typing import Any

import pytest

from shotlake.analytics.sg import strokes_gained
from shotlake.baseline import load_baseline
from shotlake.cli import main
from shotlake.connectors.arccos import ArccosConnector, HarArccosClient, parse_round
from shotlake.connectors.errors import SchemaDriftError
from shotlake.db import open_db
from shotlake.devalue import DevalueError, parse
from shotlake.ingest_api import get_cursor, sync_api

BASELINE = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"


def stringify(value: Any) -> str:
    """Minimaler devalue-Encoder (nur für Tests): jedes Element erhält einen Index."""
    values: list[Any] = []

    def add(v: Any) -> int:
        idx = len(values)
        values.append(None)
        if isinstance(v, dict):
            values[idx] = {k: add(x) for k, x in v.items()}
        elif isinstance(v, list):
            values[idx] = [add(x) for x in v]
        else:
            values[idx] = v
        return idx

    add(value)
    return json.dumps(values)


# --- devalue ------------------------------------------------------------------------------


def test_devalue_decodes_nested_shared_and_special_values():
    assert parse(stringify({"a": [1, "x", None, True], "b": {"c": 1.5}})) == {
        "a": [1, "x", None, True],
        "b": {"c": 1.5},
    }
    # geteilte Referenz, undefined (-1), Lücke (-2), NaN (-3) und -0 (-6)
    shared = parse('[{"a":1,"b":1},"v"]')
    assert shared == {"a": "v", "b": "v"}
    assert parse("[[-1,-2,-3,-6]]")[0] is None and parse("[[-1,-2,-3,-6]]")[1] is None
    import math

    assert math.isnan(parse("[[-3]]")[0])
    assert parse('[["Date","2026-01-01T00:00:00.000Z"]]') == "2026-01-01T00:00:00.000Z"
    assert parse('[["Set",1,2],"a","b"]') == ["a", "b"]
    assert parse('[["Map",1,2],"k","v"]') == {"k": "v"}
    assert parse('[["Foo",1],"x"]') == {"$type": "Foo", "value": ["x"]}


def test_devalue_cycle_and_errors():
    cyc = parse('[{"self":0}]')
    assert cyc["self"] is cyc
    assert parse("[5]") == 5  # Primitivwert als Wurzel ist gültig
    for bad in ("[]", '"x"', "[[7]]", '[{"a":9}]'):
        with pytest.raises(DevalueError):
            parse(bad)


# --- Parser -------------------------------------------------------------------------------

LAT0, LNG0 = 58.0, 13.0
M_PER_DEG = 111_194.9


def shot(club, s_terrain, e_terrain, d_start, d_end, sga, facet, pen=0, rec=False, t="00"):
    """Schlag mit Positionen `d_*` Meter südlich der Fahne."""
    return {
        "shotId": 1, "shotUUID": "u", "noOfPenalties": pen, "userStartTerrainOverride": 0,
        "clubType": club, "clubId": 1, "distanceYards": 100.0, "estimatedDistanceYards": None,
        "startLat": LAT0 - d_start / M_PER_DEG, "startLng": LNG0, "startTerrain": s_terrain,
        "endLat": LAT0 - d_end / M_PER_DEG, "endLng": LNG0, "endTerrain": e_terrain,
        "sga": sga, "sgaFacet": facet, "isRecovery": rec,
        "time": f"2026-07-01T10:{t}:00.000Z",
    }  # fmt: skip


def hole(number, par, shots, **flags):
    base = {
        "holeId": number, "par": par, "yards": 350, "handicap": number * 2, "noOfShots": len(shots),
        "isGir": False, "isFairWay": False, "isFairWayLeft": False, "isFairWayRight": False,
        "pinLat": LAT0, "pinLng": LNG0, "tees": [{"name": "Red", "distanceYards": 300}],
        "greenCenter": {"lat": LAT0, "lng": LNG0}, "shots": shots, "excludedFromStats": False,
    }  # fmt: skip
    return {**base, **flags}


def payload(round_id=11, start="2026-07-01T10:00:00.000Z", extra_hole=True):
    h1 = hole(
        1, 4,
        [
            shot(1, "tee", "fairway", 300, 100, 0.1, "driving", t="01"),
            shot(8, "fairway", "green", 100, 5, -0.2, "approaching", t="02"),
            shot(12, "green", "green", 5, 1, -0.3, "putting", t="03"),
            shot(12, "green", "green", 1, 0, 0.0, "putting", t="04"),
        ],
        isFairWay=True,
    )  # fmt: skip
    holes = [h1]
    analysis = [{"holeId": 1, "holeIdx": 0, "par": 4, "strokes": 4, "sgTotal": -0.4}]
    if extra_hole:
        h2 = hole(
            2, 3,
            [
                shot(8, "tee", "rough", 150, 30, -0.5, "approaching", pen=1, t="05"),
                shot(9, "rough", "green", 30, 4, 0.1, "short", rec=True, t="06"),
                shot(12, "green", "green", 4, 0, -0.2, "putting", t="07"),
            ],
            isFairWayLeft=True,
        )  # fmt: skip
        holes.append(h2)
        analysis.append({"holeId": 2, "holeIdx": 1, "par": 3, "strokes": 4, "sgTotal": -0.6})
    meta = {
        "roundId": round_id, "courseId": "77", "courseVersion": "3", "courseName": "Testplatz",
        "courseCity": "Teststadt", "startTime": start, "endTime": "2026-07-01T12:00:00.000Z",
        "isEnded": True, "isPrivate": False, "excludedFromStats": False, "roundVersion": 2,
        "roundTypeId": None, "par": 7, "courseParFull": 72, "noOfHoles": len(holes),
    }  # fmt: skip
    return {
        "summary": {"roundId": round_id, "startTime": start, "ended": True},
        "round": {"meta": meta, "holes": holes, "unplayedHoles": []},
        "analysis_holes": analysis,
        "overall": {"sgTotal": -1.0},
    }


def test_parse_round_maps_holes_shots_putts_and_units():
    r = parse_round(payload())
    assert r.round_id == "arccos|11" and r.course.course_id == "arccos|77-3"
    assert r.course.name == "Testplatz" and [t.name for t in r.course.tees] == ["Red"]
    assert r.course.tees[0].rating is None  # Arccos liefert kein Rating/Slope
    h1, h2 = r.holes
    assert (h1.number, h1.par, h1.stroke_index, h1.strokes, h1.putts, h1.penalties) == (
        1,
        4,
        2,
        4,
        2,
        0,
    )
    assert h1.fairway_outcome == "hit" and h2.fairway_outcome is None  # Par 3: kein Fairway-Wert
    assert (h2.putts, h2.penalties) == (1, 1)
    assert h1.pin_lat == pytest.approx(LAT0)
    assert len(r.shots) == 7
    s = r.shots[0]
    assert s.start_lie == "tee" and s.end_lie == "fairway" and s.club_raw == "arccos_clubtype_1"
    assert s.distance_m == pytest.approx(100.0 * 0.9144) and s.source_sg == 0.1
    assert s.shot_type == "TEE" and s.source_sg_facet == "driving" and s.shot_source == "ARCCOS"
    assert [x.shot_type for x in r.shots[1:4]] == ["APPROACH", "PUTT", "PUTT"]
    assert r.shots[4].shot_type == "APPROACH" and r.shots[5].shot_type == "RECOVERY"
    assert r.shots[0].shot_time_utc is not None and r.shots[0].shot_time_utc.tzinfo is not None


def test_parse_round_drift_and_unknown_terrain():
    bad = payload()
    del bad["round"]["meta"]
    with pytest.raises(SchemaDriftError):
        parse_round(bad)
    mismatch = payload()
    mismatch["analysis_holes"].pop()
    with pytest.raises(SchemaDriftError):
        parse_round(mismatch)
    odd = payload()
    odd["round"]["holes"][0]["shots"][0]["endTerrain"] = "bunker2"
    r = parse_round(odd)
    assert r.shots[0].end_lie == "other" and any("bunker2" in w for w in r.warnings)


def test_excluded_round_and_hole_flag_their_shots():
    p = payload()
    p["round"]["holes"][1]["excludedFromStats"] = True
    r = parse_round(p)
    assert not r.shots[0].exclude_from_stats and r.shots[4].exclude_from_stats
    p["round"]["meta"]["excludedFromStats"] = True
    assert parse_round(p).exclude_from_stats


# --- HAR-Client, Sync, SG, CLI ------------------------------------------------------------------


def remote_entry(name: str, result: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"type": "result", "data": stringify({"_": result, "q": {}})})
    return {
        "request": {"url": f"https://dashboard.example/_app/remote/abc123/{name}?payload=xyz"},
        "response": {"content": {"text": body}},
    }


def write_har(path: Path, rounds: list[dict[str, Any]], details: list[dict[str, Any]]) -> None:
    entries = [
        remote_entry(
            "roundsBundle", {"list": {"ok": True, "data": {"rounds": rounds, "hasMore": False}}}
        )
    ]
    entries += [
        remote_entry("roundDetailBundle", {"bundle": {"ok": True, "data": d}}) for d in details
    ]
    # Ein Eintrag mit Zugangsdaten in der URL darf nie ausgewertet werden
    entries.append(
        {
            "request": {
                "url": "https://dashboard.example/magic-link?accessKey=GEHEIM&secret=GEHEIM"
            },
            "response": {"content": {"text": "GEHEIM"}},
        }
    )
    path.write_text(json.dumps({"log": {"entries": entries}}))


def detail_of(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "round": p["round"],
        "analysis": {"holes": p["analysis_holes"], "overall": p["overall"]},
    }


def make_har(tmp_path: Path, with_missing_detail: bool = False) -> Path:
    p1, p2 = payload(11, "2026-07-01T10:00:00.000Z"), payload(12, "2026-07-02T10:00:00.000Z")
    rows = [p2["summary"], p1["summary"]]
    details = [detail_of(p1)] if with_missing_detail else [detail_of(p1), detail_of(p2)]
    har = tmp_path / "export.har"
    write_har(har, rows, details)
    return har


def test_har_client_reads_only_round_functions(tmp_path: Path):
    client = HarArccosClient(make_har(tmp_path))
    assert [r["roundId"] for r in client.rounds()] == [12, 11]  # neueste zuerst
    assert client.round_detail(11) is not None and client.round_detail(99) is None
    assert "GEHEIM" not in json.dumps(client.rounds())


def test_sync_arccos_end_to_end_and_incremental(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = ArccosConnector(client=HarArccosClient(make_har(tmp_path)), sleep=lambda _s: None)
    rep = sync_api(con, c, tmp_path / "raw")
    assert rep.status == "ok" and rep.rounds_new == 2 and rep.course_shots_new == 14
    assert get_cursor(con, "arccos") == "2026-07-02T10:00:00+00:00"
    assert con.execute(
        "select count(*) from course_shot where source_sg is not null"
    ).fetchone() == (14,)
    # zweiter Lauf: nichts Neues
    c2 = ArccosConnector(client=HarArccosClient(make_har(tmp_path)), sleep=lambda _s: None)
    rep = sync_api(con, c2, None)
    assert rep.rounds_new == 0 and rep.course_shots_new == 0
    # Roh-Dateien enthalten keine Fremdbereiche (kein `identity` o. ä.)
    raw = next((tmp_path / "raw" / "arccos").glob("*.json")).read_text()
    assert set(json.loads(raw)) == {"summary", "round", "analysis_holes", "overall"}


def test_missing_detail_is_reported_and_cursor_held_back(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = ArccosConnector(
        client=HarArccosClient(make_har(tmp_path, with_missing_detail=True)), sleep=lambda _s: None
    )
    rep = sync_api(con, c, None)
    assert rep.rounds_new == 1 and any("übersprungen" in w for w in rep.warnings)
    assert get_cursor(con, "arccos") is None  # Cursor bleibt stehen, damit nichts verloren geht


def test_no_client_reports_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for var in ("ACCESS_KEY", "SECRET", "USER_ID"):
        monkeypatch.delenv(f"SHOTLAKE_ARCCOS_{var}", raising=False)
    monkeypatch.setenv("SHOTLAKE_SOPS_FILE", str(tmp_path / "nix.json"))
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, ArccosConnector(sleep=lambda _s: None), None)
    assert rep.status == "error" and "Zugangsdaten" in rep.errors[0]


def test_sg_uses_recorded_putts_of_arccos_round(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    sync_api(
        con,
        ArccosConnector(client=HarArccosClient(make_har(tmp_path)), sleep=lambda _s: None),
        None,
    )
    res = strokes_gained(con, load_baseline(BASELINE), round_ref="11")
    assert res.complete_holes == 2  # beide Löcher vollständig, inkl. Strafschlag und Putts
    assert "green_play" not in res.totals() or res.totals()["green_play"] == 0.0
    assert res.totals()["putting"] != 0.0 and res.totals()["penalty"] == -1.0
    for h in res.holes:
        assert h.total is not None
        assert sum(h.by_category.values()) == pytest.approx(h.total, abs=1e-6)


def test_cli_sync_with_har(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    har = make_har(tmp_path)
    args = ["--data-dir", str(tmp_path / "d")]
    assert main([*args, "sync", "arccos", "--har", str(har)]) == 0
    assert "Runden neu: 2" in capsys.readouterr().out
    assert main([*args, "sync", "garmin_platform", "--har", str(har)]) == 2
