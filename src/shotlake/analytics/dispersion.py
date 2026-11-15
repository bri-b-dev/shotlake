"""Streuung je Schläger: Punkte (Seitenabweichung, Carry) und 95-%-Ellipse je Quelle/Methode.

Die Ellipse folgt der Kovarianz der Punkte (bivariate Normalverteilung als Näherung): Mittelpunkt
ist der Mittelwert, Achsen sind die Eigenrichtungen, Skalierung `sqrt(chi²(0,95; 2) · λ)`.
Gefiltert wird wie bei den Distanzen (Ausreißer, Teilschläge). Seite: rechts positiv.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import duckdb

from ..quality import EXCLUDE_FLAGS

CHI2_95_2DOF = 5.991464547107979
MIN_POINTS = 8


@dataclass(frozen=True)
class Ellipse:
    cx: float  # Mittelpunkt Seite (m)
    cy: float  # Mittelpunkt Carry (m)
    semi_major: float
    semi_minor: float
    angle_rad: float  # Richtung der Hauptachse im (Seite, Carry)-System

    def outline(self, n: int = 72) -> list[tuple[float, float]]:
        ca, sa = math.cos(self.angle_rad), math.sin(self.angle_rad)
        pts = []
        for i in range(n):
            t = 2 * math.pi * i / n
            u, v = self.semi_major * math.cos(t), self.semi_minor * math.sin(t)
            pts.append((self.cx + u * ca - v * sa, self.cy + u * sa + v * ca))
        return pts


@dataclass(frozen=True)
class ScatterGroup:
    club_type: str
    source: str
    method: str
    points: list[tuple[float, float]]  # (Seite m, Carry m)
    ellipse: Ellipse | None


def ellipse_95(points: list[tuple[float, float]]) -> Ellipse | None:
    """95-%-Ellipse; None bei weniger als MIN_POINTS Punkten oder ohne Streuung."""
    n = len(points)
    if n < MIN_POINTS:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points) / (n - 1)
    syy = sum((p[1] - my) ** 2 for p in points) / (n - 1)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points) / (n - 1)
    half_trace = (sxx + syy) / 2
    root = math.sqrt(max(((sxx - syy) / 2) ** 2 + sxy**2, 0.0))
    l1, l2 = half_trace + root, half_trace - root
    if l1 <= 0 or l2 <= 0:
        return None
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    k = math.sqrt(CHI2_95_2DOF)
    return Ellipse(mx, my, k * math.sqrt(l1), k * math.sqrt(l2), angle)


def scatter_groups(
    con: duckdb.DuckDBPyConnection,
    club_type: str,
    *,
    source: str | None = None,
    since: date | None = None,
    until: date | None = None,
    min_speed_ratio: float | None = None,
) -> list[ScatterGroup]:
    where = [
        "sh.club_type = ?",
        "sh.carry_m is not null",
        "sh.carry_side_m is not null",
    ]
    params: list[object] = [club_type]
    if source:
        where.append("sh.source = ?")
        params.append(source)
    if since:
        where.append("se.start_utc >= ?")
        params.append(datetime.combine(since, time.min))
    if until:
        where.append("se.start_utc < ?")
        params.append(datetime.combine(until + timedelta(days=1), time.min))
    flags = ", ".join(f"'{f}'" for f in EXCLUDE_FLAGS)
    where.append(f"not list_has_any(sh.flags, [{flags}])")
    if min_speed_ratio is not None:
        where.append(
            "(sh.ball_speed_ms is null or sh.ball_speed_ms >= ? * (select median(b.ball_speed_ms) "
            "from shot b where b.club_type = sh.club_type and "
            "b.measurement_method = sh.measurement_method and b.ball_speed_ms is not null))"
        )
        params.append(min_speed_ratio)
    rows = con.execute(
        "select sh.source, sh.measurement_method, sh.carry_side_m, sh.carry_m "
        "from shot sh join session se using (session_id) "
        f"where {' and '.join(where)} order by sh.source, sh.measurement_method, se.start_utc",
        params,
    ).fetchall()
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for src, method, side, carry in rows:
        grouped.setdefault((src, method), []).append((side, carry))
    return [
        ScatterGroup(club_type, src, method, pts, ellipse_95(pts))
        for (src, method), pts in sorted(grouped.items())
    ]
