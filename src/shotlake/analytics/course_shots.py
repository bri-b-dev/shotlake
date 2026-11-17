"""Auswertung der Schläge auf dem Platz: Distanz je Schläger, Lagen, Abschlag- und Grüntreffer.

`distance_m` der Plattform-Quellen ist die per GPS gemessene Strecke zwischen Start und Ende
des Schlags (inkl. Rollen), **kein** Carry; sie ist nicht mit Range-Carry vergleichbar.
Schläger werden über `club_map` (Nutzerzuordnung) auf kanonische Typen abgebildet, sonst bleibt
die Quellenbezeichnung stehen.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import duckdb

_BASE = """
    from course_shot s
    join round r using (round_id)
    left join club_map m on m.source = r.source and m.club_ref = s.club_raw
    where not s.exclude_from_stats
"""


def _filters(
    since: date | None,
    until: date | None,
    round_ref: str | None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> tuple[str, list[object]]:
    where = ""
    params: list[object] = []
    if since:
        where += " and r.start_utc >= ?"
        params.append(datetime.combine(since, time.min))
    if until:
        where += " and r.start_utc < ?"
        params.append(datetime.combine(until + timedelta(days=1), time.min))
    if round_ref:
        where += " and (r.round_id = ? or r.round_key = ?)"
        params += [round_ref, round_ref]
    if source:
        where += " and r.source = ?"
        params.append(source)
    if exclude_rounds:  # nicht führende Dubletten (Dedup)
        where += " and not list_contains(?, r.round_id)"
        params.append(list(exclude_rounds))
    return where, params


@dataclass(frozen=True)
class ClubDistance:
    club: str  # kanonischer Typ oder Quellenbezeichnung
    mapped: bool
    n: int
    median_m: float | None
    p10_m: float | None
    p90_m: float | None
    max_m: float | None
    warning: str | None


def club_distances(
    con: duckdb.DuckDBPyConnection,
    *,
    min_n: int = 5,
    since: date | None = None,
    until: date | None = None,
    round_ref: str | None = None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> list[ClubDistance]:
    """Schlagdistanz je Schläger; Schläge ohne Schläger und vom Grün (Putts) bleiben außen vor."""
    extra, params = _filters(since, until, round_ref, source, exclude_rounds)
    rows = con.execute(
        f"""
        select coalesce(s.club_type, m.club_type, s.club_raw) club,
               bool_and(coalesce(s.club_type, m.club_type) is not null),
               count(*), median(s.distance_m), quantile_cont(s.distance_m, 0.1),
               quantile_cont(s.distance_m, 0.9), max(s.distance_m)
        {_BASE} and s.club_raw is not null and s.distance_m is not null
            and coalesce(s.start_lie, '') <> 'green' {extra}
        group by 1 order by 4 desc
        """,
        params,
    ).fetchall()
    return [
        ClubDistance(
            r[0],
            bool(r[1]),
            r[2],
            r[3],
            r[4],
            r[5],
            r[6],
            "insufficient_data" if r[2] < min_n else None,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class LieShare:
    lie: str | None
    n: int
    share: float


def _shares(rows: list[tuple[str | None, int]]) -> list[LieShare]:
    total = sum(n for _, n in rows)
    return [LieShare(lie, n, n / total) for lie, n in rows] if total else []


def start_lies(
    con: duckdb.DuckDBPyConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    round_ref: str | None = None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> list[LieShare]:
    """Verteilung der Lagen, aus denen geschlagen wurde (ohne Putts auf dem Grün)."""
    extra, params = _filters(since, until, round_ref, source, exclude_rounds)
    rows = con.execute(
        f"select s.start_lie, count(*) {_BASE} and coalesce(s.start_lie,'') <> 'green' {extra} "
        "group by 1 order by 2 desc",
        params,
    ).fetchall()
    return _shares(rows)


def tee_shot_outcomes(
    con: duckdb.DuckDBPyConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    round_ref: str | None = None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> list[LieShare]:
    """Wo Abschläge (shot_type TEE) landen; NULL = Lage unbekannt."""
    extra, params = _filters(since, until, round_ref, source, exclude_rounds)
    rows = con.execute(
        f"select s.end_lie, count(*) {_BASE} and s.shot_type = 'TEE' {extra} "
        "group by 1 order by 2 desc",
        params,
    ).fetchall()
    return _shares(rows)


def approach_green_rate(
    con: duckdb.DuckDBPyConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    round_ref: str | None = None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> dict[str, tuple[int, int]]:
    """Grüntreffer nach Annäherungsschlägen je Start-Lage: Lage -> (Treffer, Schläge)."""
    extra, params = _filters(since, until, round_ref, source, exclude_rounds)
    rows = con.execute(
        f"""select coalesce(s.start_lie, 'unbekannt'), count(*) filter (where s.end_lie = 'green'),
                   count(*)
            {_BASE} and s.shot_type = 'APPROACH' and s.end_lie is not null {extra} group by 1""",
        params,
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}
