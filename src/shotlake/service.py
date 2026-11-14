"""Service-Schicht: gemeinsame Abfragen für Weboberfläche (und später MCP-Server)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from .analytics.score import PCC_MAX, PCC_MIN, RoundScore, round_scores
from .analytics.sg import HoleSG, ShotSG, strokes_gained
from .baseline import Baseline
from .connectors.registry import API_CONNECTORS, CONNECTORS


@dataclass(frozen=True)
class SourceStatus:
    id: str
    mode: str
    risk: str
    description: str
    rounds: int
    sessions: int
    range_shots: int
    course_shots: int
    last_status: str | None
    last_run: datetime | None


def source_statuses(con: duckdb.DuckDBPyConnection) -> list[SourceStatus]:
    meta: dict[str, tuple[str, str, str]] = {
        c.id: (c.mode, c.risk, c.description) for c in CONNECTORS.values()
    }
    for factory in API_CONNECTORS.values():
        a = factory()
        meta[a.id] = (a.mode, a.risk, a.description)

    def counts(sql: str) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in con.execute(sql).fetchall()}

    rounds = counts("select source, count(*) from round group by 1")
    sessions = counts("select source, count(*) from session group by 1")
    range_shots = counts("select source, count(*) from shot group by 1")
    course_shots = counts(
        "select r.source, count(*) from course_shot s join round r using (round_id) group by 1"
    )
    last = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "select source, status, finished_at from import_run "
            "qualify row_number() over (partition by source order by started_at desc) = 1"
        ).fetchall()
    }
    out = []
    for sid, (mode, risk, desc) in sorted(meta.items()):
        status, finished = last.get(sid, (None, None))
        out.append(
            SourceStatus(
                sid, mode, risk, desc, rounds.get(sid, 0), sessions.get(sid, 0),
                range_shots.get(sid, 0), course_shots.get(sid, 0), status, finished,
            )
        )  # fmt: skip
    return out


def default_source(con: duckdb.DuckDBPyConnection) -> str | None:
    """Quelle mit den meisten Runden (für Voreinstellungen, solange es kein Dedup gibt)."""
    row = con.execute(
        "select source from round group by 1 order by count(*) desc, source limit 1"
    ).fetchone()
    return None if row is None else str(row[0])


def round_sources(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("select distinct source from round order by 1").fetchall()]


@dataclass(frozen=True)
class RoundDetail:
    score: RoundScore
    source: str
    holes: list[dict[str, Any]]
    sg_holes: dict[int, HoleSG]
    sg_shots: dict[int, list[ShotSG]]
    shots: list[dict[str, Any]]
    inputs: dict[str, Any]
    raw_hash: str
    import_run_id: str
    tee_name: str | None
    baseline_id: str


def round_detail(
    con: duckdb.DuckDBPyConnection, ref: str, baseline: Baseline
) -> RoundDetail | None:
    found = round_scores(con, round_ref=ref)
    if len(found) != 1:
        return None
    score = found[0]
    rid = score.round_id
    meta = con.execute(
        "select source, raw_hash, import_run_id, tee_name from round where round_id = ?", [rid]
    ).fetchone()
    assert meta is not None

    def rows(sql: str) -> list[dict[str, Any]]:
        cur = con.execute(sql, [rid])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    sg = strokes_gained(con, baseline, round_ref=rid)
    sg_shots: dict[int, list[ShotSG]] = {}
    for s in sg.shots:
        sg_shots.setdefault(s.hole, []).append(s)
    inputs = (rows("select * from round_input where round_id = ?") or [{}])[0]
    return RoundDetail(
        score=score,
        source=str(meta[0]),
        holes=rows("select * from hole where round_id = ? order by hole_number"),
        sg_holes={h.hole: h for h in sg.holes},
        sg_shots=sg_shots,
        shots=rows(
            "select s.*, coalesce(m.club_type, s.club_raw) as club from course_shot s "
            "join round r using (round_id) left join club_map m "
            "on m.source = r.source and m.club_ref = s.club_raw "
            "where s.round_id = ? order by s.hole_number, s.shot_order"
        ),
        inputs=inputs,
        raw_hash=str(meta[1]),
        import_run_id=str(meta[2]),
        tee_name=meta[3],
        baseline_id=baseline.id,
    )


def set_round_inputs(
    con: duckdb.DuckDBPyConnection,
    round_id: str,
    *,
    relevant: str,
    hi: str,
    pcc: str,
    ags: str,
) -> list[str]:
    """Nutzereingaben zu einer Runde speichern; leere Felder löschen den Wert. Liefert Fehler."""
    errors: list[str] = []
    choices: dict[str, bool | None] = {"yes": True, "no": False, "auto": None}
    if relevant not in choices:
        errors.append("Handicap-Relevanz: ungültiger Wert")
    rel = choices.get(relevant)

    def number(text: str, label: str, lo: float, hi_: float, as_int: bool = False) -> Any:
        text = text.strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            errors.append(f"{label}: keine Zahl")
            return None
        if not lo <= value <= hi_:
            errors.append(f"{label}: erlaubt sind {lo:g} bis {hi_:g}")
            return None
        if as_int:
            if value != int(value):
                errors.append(f"{label}: ganze Zahl erwartet")
                return None
            return int(value)
        return value

    hi_value = number(hi, "Handicap-Index zum Spielzeitpunkt", -10.0, 54.0)
    pcc_value = number(pcc, "PCC", PCC_MIN, PCC_MAX)
    ags_value = number(ags, "AGS", 9, 250, as_int=True)
    if errors:
        return errors
    con.execute(
        "insert or replace into round_input "
        "(round_id, handicap_relevant, handicap_index_at_play, pcc, ags_override, updated_at) "
        "values (?,?,?,?,?, now()::timestamp)",
        [round_id, rel, hi_value, pcc_value, ags_value],
    )
    return []
