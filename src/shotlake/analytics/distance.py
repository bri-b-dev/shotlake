"""Länge, Dispersion und Gapping je Schläger (Quelle und Messmethode getrennt)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise

import duckdb

from ..clubs import club_sort_key
from ..quality import EXCLUDE_FLAGS


@dataclass(frozen=True)
class ClubStats:
    club_type: str
    source: str
    method: str
    mixed: bool
    n: int
    carry_median_m: float | None
    carry_mean_m: float | None
    carry_p10_m: float | None
    carry_p90_m: float | None
    carry_std_m: float | None
    total_median_m: float | None
    n_side: int
    side_bias_m: float | None  # rechts positiv
    side_std_m: float | None
    left_share: float | None
    center_share: float | None
    right_share: float | None
    warning: str | None


@dataclass(frozen=True)
class GapRow:
    longer_club: str
    shorter_club: str
    source: str
    method: str
    longer_median_m: float
    shorter_median_m: float
    gap_m: float
    status: str  # ok | overlap | gap_too_small | gap_too_large


# Schläge mit deutlich geringerer Ballgeschwindigkeit als der Median des Schlägers (je Methode)
# sind Teilschläge, Chips oder Probeschwünge und verfälschen Distanzen. Die Auswertungen
# (CLI, Web, MCP) übergeben diesen Wert; die Bibliotheksfunktionen filtern nur auf Wunsch.
DEFAULT_MIN_SPEED_RATIO = 0.7


def club_stats(
    con: duckdb.DuckDBPyConnection,
    *,
    club_type: str | None = None,
    source: str | None = None,
    method: str | None = None,
    since: date | None = None,
    until: date | None = None,
    indoor: bool | None = None,
    min_n: int = 10,
    include_flagged: bool = False,
    combine: bool = False,
    corridor_m: float = 5.0,
    min_speed_ratio: float | None = None,
) -> list[ClubStats]:
    where = ["sh.club_type is not null", "sh.carry_m is not null"]
    params: list[object] = []
    if club_type:
        where.append("sh.club_type = ?")
        params.append(club_type)
    if source:
        where.append("sh.source = ?")
        params.append(source)
    if method:
        where.append("sh.measurement_method = ?")
        params.append(method)
    if since:
        where.append("se.start_utc >= ?")
        params.append(datetime.combine(since, time.min))
    if until:
        where.append("se.start_utc < ?")
        params.append(datetime.combine(until + timedelta(days=1), time.min))
    if indoor is not None:
        where.append("sh.indoor = ?")
        params.append(indoor)
    if not include_flagged:
        flags = ", ".join(f"'{f}'" for f in EXCLUDE_FLAGS)
        where.append(f"not list_has_any(sh.flags, [{flags}])")

    if min_speed_ratio is not None:
        where.append(
            "(sh.ball_speed_ms is null or sh.ball_speed_ms >= ? * (select median(b.ball_speed_ms) "
            "from shot b where b.club_type = sh.club_type and "
            "b.measurement_method = sh.measurement_method and b.ball_speed_ms is not null))"
        )
        params.append(min_speed_ratio)

    if combine:
        group = "sh.club_type"
        src = "string_agg(distinct sh.source, ',' order by sh.source)"
        meth = "string_agg(distinct sh.measurement_method, ',' order by sh.measurement_method)"
        mixed = "(count(distinct sh.source) > 1 or count(distinct sh.measurement_method) > 1)"
    else:
        group = "sh.club_type, sh.source, sh.measurement_method"
        src, meth, mixed = "any_value(sh.source)", "any_value(sh.measurement_method)", "false"

    sql = f"""
        select sh.club_type, {src}, {meth}, {mixed},
               count(*),
               median(sh.carry_m), avg(sh.carry_m),
               quantile_cont(sh.carry_m, 0.1), quantile_cont(sh.carry_m, 0.9),
               stddev_samp(sh.carry_m), median(sh.total_m),
               count(sh.carry_side_m), avg(sh.carry_side_m), stddev_samp(sh.carry_side_m),
               count(*) filter (where sh.carry_side_m < ?),
               count(*) filter (where sh.carry_side_m > ?)
        from shot sh join session se using (session_id)
        where {" and ".join(where)}
        group by {group}
    """
    rows = con.execute(sql, [-corridor_m, corridor_m, *params]).fetchall()
    out: list[ClubStats] = []
    for r in rows:
        n, n_side = r[4], r[11]
        left, right = r[14], r[15]
        out.append(
            ClubStats(
                club_type=r[0],
                source=r[1],
                method=r[2],
                mixed=bool(r[3]),
                n=n,
                carry_median_m=r[5],
                carry_mean_m=r[6],
                carry_p10_m=r[7],
                carry_p90_m=r[8],
                carry_std_m=r[9],
                total_median_m=r[10],
                n_side=n_side,
                side_bias_m=r[12],
                side_std_m=r[13],
                left_share=left / n_side if n_side else None,
                right_share=right / n_side if n_side else None,
                center_share=(n_side - left - right) / n_side if n_side else None,
                warning="insufficient_data" if n < min_n else None,
            )
        )
    out.sort(key=lambda s: (club_sort_key(s.club_type), s.source, s.method))
    return out


def gapping(
    con: duckdb.DuckDBPyConnection,
    *,
    min_n: int = 10,
    overlap_m: float = 4.0,
    small_gap_m: float = 8.0,
    large_gap_m: float = 18.0,
    source: str | None = None,
    method: str | None = None,
    since: date | None = None,
    until: date | None = None,
    indoor: bool | None = None,
    include_flagged: bool = False,
    min_speed_ratio: float | None = None,
) -> list[GapRow]:
    """Carry-Mediane je Quelle/Methode ordnen und Lücken/Überlappungen bewerten.

    Nur Schläger mit mindestens `min_n` Schlägen gehen ein (sonst keine belastbare Aussage).
    """
    stats = club_stats(
        con,
        source=source,
        method=method,
        since=since,
        until=until,
        indoor=indoor,
        min_n=min_n,
        include_flagged=include_flagged,
        combine=False,
        min_speed_ratio=min_speed_ratio,
    )
    groups: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for s in stats:
        if s.warning is None and s.carry_median_m is not None:
            groups.setdefault((s.source, s.method), []).append((s.carry_median_m, s.club_type))
    rows: list[GapRow] = []
    for (src, meth), items in sorted(groups.items()):
        items.sort(reverse=True)
        for (longer_m, longer_club), (shorter_m, shorter_club) in pairwise(items):
            gap = longer_m - shorter_m
            if gap < overlap_m:
                status = "overlap"
            elif gap < small_gap_m:
                status = "gap_too_small"
            elif gap > large_gap_m:
                status = "gap_too_large"
            else:
                status = "ok"
            rows.append(
                GapRow(longer_club, shorter_club, src, meth, longer_m, shorter_m, gap, status)
            )
    return rows
