"""Strokes Gained (selbst berechnet, `computed`) aus Lage und Distanz zur Fahne.

SG je Schlag = E(Start) - E(Ende) - 1; Einlochen hat E = 0; Strafschläge zählen einzeln.
E kommt aus einer versionierten Baseline (siehe `baseline.py`). Eine Zeile gilt nur, wenn das Loch
**vollständig** erfasst ist (Schlagzahl passt zu Score, Putts und Strafschlägen; Lagen, Koordinaten
und Fahnenposition vorhanden). Unvollständige Löcher werden mit Grund ausgewiesen und nie
geschätzt. Quellen-SG (z. B. Arccos) ist `source_provided` und wird hier nicht eingemischt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import duckdb

from ..baseline import Baseline
from ..geo import haversine_m

APPROACH_MIN_M = (
    90.0  # ab dieser Entfernung zur Fahne zählt ein Schlag (nicht vom Tee) als Approach
)
# Liegt das Ende des letzten Schlags (Garmin: Grün) näher als so an der Fahnenposition, ist es keine
# echte Ballposition (die Quelle setzt dort die Fahne): erste Putt-Distanz unbekannt.
SNAP_M = 1.0
CATEGORIES = ("tee", "approach", "short", "green_play", "putting", "penalty")


@dataclass(frozen=True)
class ShotSG:
    round_id: str
    hole: int
    order: int
    category: str
    start_lie: str
    start_m: float
    end_lie: str
    end_m: float
    sg: float


@dataclass(frozen=True)
class HoleSG:
    round_id: str
    hole: int
    complete: bool
    reason: str | None
    total: float | None
    by_category: dict[str, float]


@dataclass(frozen=True)
class _Hole:
    round_id: str
    number: int
    par: int | None
    strokes: int | None
    putts: int | None
    penalties: int | None
    pin: tuple[float, float] | None


@dataclass(frozen=True)
class _Shot:
    order: int
    start_lie: str | None
    end_lie: str | None
    start: tuple[float, float] | None
    end: tuple[float, float] | None
    excluded: bool


def _incomplete(h: _Hole, reason: str) -> tuple[HoleSG, list[ShotSG]]:
    return HoleSG(h.round_id, h.number, False, reason, None, {}), []


def hole_sg(h: _Hole, shots: list[_Shot], baseline: Baseline) -> tuple[HoleSG, list[ShotSG]]:
    if h.strokes is None or h.putts is None or h.penalties is None:
        return _incomplete(h, "Schläge, Putts oder Strafschläge nicht erfasst")
    # Schläge, die auf dem Grün beginnen, sind Putts (Arccos); Garmin erfasst sie nicht als Schläge
    everything = sorted(shots, key=lambda s: s.order)
    putt_shots = [s for s in everything if s.start_lie == "green"]
    ordered = [s for s in everything if s.start_lie != "green"]
    expected_n = h.strokes - h.putts - h.penalties
    if expected_n < 1 or len(ordered) != expected_n:
        return _incomplete(h, f"{len(ordered)} Schläge erfasst, {expected_n} erwartet")
    if putt_shots and len(putt_shots) != h.putts:
        return _incomplete(h, f"{len(putt_shots)} Putt-Schläge erfasst, {h.putts} Putts")
    if h.pin is None:
        return _incomplete(h, "Fahnenposition fehlt")
    for s in everything:
        if s.excluded:
            return _incomplete(h, "Schlag von der Quelle ausgeschlossen")
        if s.start is None or s.end is None or not s.start_lie or not s.end_lie:
            return _incomplete(h, "Lage oder Koordinaten fehlen")
    if ordered[0].start_lie != "tee":
        return _incomplete(h, "erster Schlag nicht vom Tee")
    last = ordered[-1]
    if h.putts > 0 and last.end_lie != "green":
        return _incomplete(h, "Putts, aber letzter Schlag endet nicht auf dem Grün")

    def dist(point: tuple[float, float] | None) -> float:
        assert point is not None and h.pin is not None
        return haversine_m(point[0], point[1], h.pin[0], h.pin[1])

    def exp(lie: str | None, d: float) -> float | None:
        return baseline.expected(lie or "", d)

    e_tee = exp("tee", dist(ordered[0].start))
    # Putt-Distanz: bei erfassten Putts die echte Startposition des ersten Putts. Sonst gilt die
    # Endposition des letzten Schlags; liegt sie auf der Fahne, ist sie keine echte Ballposition,
    # und Putts sind nicht vom letzten Schlag trennbar (green_play).
    snapped = False
    if h.putts == 0:
        first_putt: float | None = 0.0
    elif putt_shots:
        first_putt = exp("green", dist(putt_shots[0].start))
    elif dist(last.end) < SNAP_M:
        snapped, first_putt = True, float(h.putts)
    else:
        first_putt = exp("green", dist(last.end))
    if e_tee is None or first_putt is None:
        return _incomplete(h, "Baseline ohne Tabelle für die Lage")

    out: list[ShotSG] = []
    by_cat: dict[str, float] = defaultdict(float)
    for i, s in enumerate(ordered):
        d_start, d_end = dist(s.start), dist(s.end)
        e_start = exp(s.start_lie, d_start)
        e_end = first_putt if i == len(ordered) - 1 else exp(s.end_lie, d_end)
        if e_start is None or e_end is None:
            return _incomplete(h, f"Baseline ohne Tabelle für Lage {s.start_lie}/{s.end_lie}")
        is_last = i == len(ordered) - 1
        if snapped and is_last:
            category = "green_play"
        elif i == 0 and (h.par or 0) >= 4:
            category = "tee"
        else:
            category = "approach" if d_start >= APPROACH_MIN_M else "short"
        sg = e_start - e_end - 1
        by_cat[category] += sg
        out.append(
            ShotSG(
                h.round_id, h.number, s.order, category,
                s.start_lie or "", d_start, s.end_lie or "", d_end, sg,
            )
        )  # fmt: skip
    if h.putts > 0 and not snapped:
        by_cat["putting"] = first_putt - h.putts
    if h.penalties:
        by_cat["penalty"] = -float(h.penalties)
    total = e_tee - h.strokes
    return HoleSG(h.round_id, h.number, True, None, total, dict(by_cat)), out


@dataclass(frozen=True)
class SGResult:
    baseline_id: str
    holes: list[HoleSG]
    shots: list[ShotSG]

    def totals(self) -> dict[str, float]:
        sums: dict[str, float] = dict.fromkeys(CATEGORIES, 0.0)
        for h in self.holes:
            for cat, v in h.by_category.items():
                sums[cat] += v
        return sums

    @property
    def complete_holes(self) -> int:
        return sum(1 for h in self.holes if h.complete)


def strokes_gained(
    con: duckdb.DuckDBPyConnection,
    baseline: Baseline,
    *,
    since: date | None = None,
    until: date | None = None,
    round_ref: str | None = None,
    source: str | None = None,
    exclude_rounds: Collection[str] | None = None,
) -> SGResult:
    where = ["1=1"]
    params: list[object] = []
    if since:
        where.append("r.start_utc >= ?")
        params.append(datetime.combine(since, time.min))
    if until:
        where.append("r.start_utc < ?")
        params.append(datetime.combine(until + timedelta(days=1), time.min))
    if round_ref:
        where.append("(r.round_id = ? or r.round_key = ?)")
        params += [round_ref, round_ref]
    if source:
        where.append("r.source = ?")
        params.append(source)
    if not source and not round_ref:
        where.append("not r.simulated")  # Sim-Runden nur gezielt (Quelle/Runde), nie gemischt
    if exclude_rounds:
        where.append("not list_contains(?, r.round_id)")
        params.append(list(exclude_rounds))
    holes_rows = con.execute(
        f"""select h.round_id, h.hole_number, h.par, h.strokes, h.putts, h.penalties,
                   h.pin_lat, h.pin_lon
            from hole h join round r using (round_id)
            where {" and ".join(where)} and h.strokes is not null
            order by r.start_utc, h.hole_number""",
        params,
    ).fetchall()
    shot_rows = con.execute(
        f"""select s.round_id, s.hole_number, s.shot_order, s.start_lie, s.end_lie,
                   s.start_lat, s.start_lon, s.end_lat, s.end_lon, s.exclude_from_stats
            from course_shot s join round r using (round_id)
            where {" and ".join(where)}""",
        params,
    ).fetchall()
    by_hole: dict[tuple[str, int], list[_Shot]] = defaultdict(list)
    for r in shot_rows:
        start = (r[5], r[6]) if r[5] is not None and r[6] is not None else None
        end = (r[7], r[8]) if r[7] is not None and r[8] is not None else None
        by_hole[(r[0], r[1])].append(_Shot(r[2], r[3], r[4], start, end, bool(r[9])))
    holes: list[HoleSG] = []
    shots: list[ShotSG] = []
    for r in holes_rows:
        pin = (r[6], r[7]) if r[6] is not None and r[7] is not None else None
        h = _Hole(r[0], r[1], r[2], r[3], r[4], r[5], pin)
        sg_hole, sg_shots = hole_sg(h, by_hole.get((h.round_id, h.number), []), baseline)
        holes.append(sg_hole)
        shots.extend(sg_shots)
    return SGResult(baseline.id, holes, shots)
