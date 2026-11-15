"""Kennzahlenkatalog: gleiche Definitionen für Trend, Statistik und Zeitraumvergleich.

Runden-Kennzahlen liefern einen Wert je Runde, Schlag-Kennzahlen einen je Schlag. Fehlende Werte
(NULL) werden ausgelassen, nie als 0 gezählt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import duckdb

from ..baseline import Baseline
from ..dedup import Scope
from ..quality import EXCLUDE_FLAGS
from .score import RoundScore, round_scores
from .sg import CATEGORIES, SGResult, strokes_gained

MIN_SG_HOLES = (
    5  # Runden mit weniger vollständig erfassten Löchern gehen nicht in SG-Kennzahlen ein
)


@dataclass(frozen=True)
class Filters:
    source: str | None = None
    since: date | None = None
    until: date | None = None
    only_relevant: bool = False
    club: str | None = None  # nur Schlag-Kennzahlen
    last_n: int | None = None  # nur die letzten N Beobachtungen


@dataclass(frozen=True)
class Observation:
    value: float
    when: datetime
    source: str
    ref: str  # Runden-ID bzw. Session-ID


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    unit: str
    level: str  # round | shot
    better: str | None  # lower | higher | None
    min_n: int
    extract: Callable[[duckdb.DuckDBPyConnection, Filters, Baseline, Context], list[Observation]]
    note: str = ""
    stat: str = "mean"
    sources_hint: tuple[str, ...] = field(default=())
    group: str = (
        "score"  # Dedup-Feldgruppe: score (Brutto/Differential) oder shots (Lochdetails/SG)
    )


class Context:
    """Zwischenspeicher je Anfrage: Runden und SG werden je Quelle nur einmal berechnet.

    Zeiträume werden danach in Python gefiltert; der Speicher gilt nur für die Lebensdauer einer
    Anfrage (kein globaler Cache, keine veralteten Werte).
    """

    def __init__(self, scope: Scope | None = None) -> None:
        """`scope`: Dedup-Sicht. Ohne Quellenfilter zählen nur führende Runden je Feldgruppe."""
        self.scope = scope
        self._rounds: dict[tuple[str | None, bool], list[RoundScore]] = {}
        self._sg: dict[tuple[int, str | None], SGResult] = {}

    def rounds(
        self, con: duckdb.DuckDBPyConnection, f: Filters, group: str = "score"
    ) -> list[RoundScore]:
        key = (f.source, f.only_relevant)
        if key not in self._rounds:
            self._rounds[key] = round_scores(
                con,
                source=f.source,
                only_relevant=f.only_relevant,
                donors=self.scope.donors if self.scope else None,
            )
        rows = self._rounds[key]
        if f.source is None:  # Sim-Runden nie mit realen Runden mischen (nur per Quelle abrufbar)
            rows = [r for r in rows if not r.simulated]
        if self.scope is not None and f.source is None:
            dropped = self.scope.exclude(group)
            rows = [r for r in rows if r.round_id not in dropped]
        return [r for r in rows if _in_range(r.start_utc, f)]

    def sg(
        self, con: duckdb.DuckDBPyConnection, baseline: Baseline, source: str | None
    ) -> SGResult:
        key = (id(baseline), source)
        if key not in self._sg:
            dropped = self.scope.exclude("shots") if self.scope and source is None else None
            self._sg[key] = strokes_gained(con, baseline, source=source, exclude_rounds=dropped)
        return self._sg[key]


def _in_range(when: datetime, f: Filters) -> bool:
    if f.since and when < datetime.combine(f.since, time.min):
        return False
    return not (f.until and when >= datetime.combine(f.until + timedelta(days=1), time.min))


def _from_rounds(
    pick: Callable[[RoundScore], float | None], group: str = "score"
) -> Callable[..., list[Observation]]:
    def extract(
        con: duckdb.DuckDBPyConnection, f: Filters, _b: Baseline, ctx: Context
    ) -> list[Observation]:
        out = []
        for r in ctx.rounds(con, f, group):
            value = pick(r)
            if value is not None:
                out.append(Observation(value, r.start_utc, r.round_id.split("|")[0], r.round_id))
        return out

    return extract


def _rate(hits: int, recorded: int) -> float | None:
    return hits / recorded if recorded else None


def sg_round_observations(
    con: duckdb.DuckDBPyConnection,
    f: Filters,
    baseline: Baseline,
    res: SGResult | None = None,
    ctx: Context | None = None,
) -> dict[str, list[Observation]]:
    """SG je Loch und Runde für „total" und jede Kategorie (nur Runden mit genug Löchern).

    Rechnet Strokes Gained einmal; `res` bzw. `ctx` erlauben, Vorhandenes wiederzuverwenden.
    """
    ctx = ctx or Context()
    res = res or ctx.sg(con, baseline, f.source)
    rounds = {
        r.round_id: r
        for r in ctx.rounds(con, Filters(f.source, f.since, f.until, f.only_relevant), "shots")
    }
    totals: dict[str, list[float]] = defaultdict(list)
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for h in res.holes:
        if not h.complete or h.total is None or h.round_id not in rounds:
            continue
        totals[h.round_id].append(h.total)
        sums[h.round_id]["total"] += h.total
        for cat in CATEGORIES:
            sums[h.round_id][cat] += h.by_category.get(cat, 0.0)
    out: dict[str, list[Observation]] = {key: [] for key in ("total", *CATEGORIES)}
    for rid, holes in totals.items():
        if len(holes) < MIN_SG_HOLES:
            continue
        r = rounds[rid]
        for key in out:
            value = sums[rid][key] / len(holes)
            out[key].append(Observation(value, r.start_utc, rid.split("|")[0], rid))
    return {key: sorted(v, key=lambda o: o.when) for key, v in out.items()}


def _sg_extract(category: str | None) -> Callable[..., list[Observation]]:
    def extract(
        con: duckdb.DuckDBPyConnection, f: Filters, baseline: Baseline, ctx: Context
    ) -> list[Observation]:
        return sg_round_observations(con, f, baseline, ctx=ctx)[category or "total"]

    return extract


def _carry(
    con: duckdb.DuckDBPyConnection, f: Filters, _b: Baseline, _ctx: Context
) -> list[Observation]:
    where = ["sh.carry_m is not null", "sh.club_type is not null"]
    params: list[object] = []
    if f.club:
        where.append("sh.club_type = ?")
        params.append(f.club)
    if f.source:
        where.append("sh.source = ?")
        params.append(f.source)
    flags = ", ".join(f"'{x}'" for x in EXCLUDE_FLAGS)
    where.append(f"not list_has_any(sh.flags, [{flags}])")
    rows = con.execute(
        "select sh.carry_m, se.start_utc, sh.source, sh.session_id from shot sh "
        f"join session se using (session_id) where {' and '.join(where)} order by se.start_utc",
        params,
    ).fetchall()
    return [Observation(r[0], r[1], r[2], r[3]) for r in rows if _in_range(r[1], f)]


METRICS: dict[str, Metric] = {
    m.id: m
    for m in (
        Metric("differential", "Score Differential", "Punkte", "round", "lower", 5,
               _from_rounds(lambda r: r.differential),
               "9-Loch-Runden hochgerechnet; kein Handicap-Index"),
        Metric("gross", "Brutto-Score", "Schläge", "round", None, 5,
               _from_rounds(lambda r: float(r.gross) if r.gross is not None else None),
               "nicht zwischen 9- und 18-Loch-Runden vergleichbar; besser to_par_per_hole nutzen"),
        Metric("to_par_per_hole", "Schläge zu Par pro Loch", "Schläge/Loch", "round", "lower", 5,
               _from_rounds(
                   lambda r: r.to_par / r.holes_played if r.to_par is not None else None
               )),
        Metric("putts_per_hole", "Putts pro Loch", "Putts/Loch", "round", "lower", 5,
               _from_rounds(
                   lambda r: r.putts / r.holes_played if r.putts is not None else None, "shots"
               ), group="shots"),
        Metric("fir_rate", "Fairways getroffen", "Anteil", "round", "higher", 5,
               _from_rounds(lambda r: _rate(r.fir_hits, r.fir_recorded), "shots"),
               group="shots"),
        Metric("gir_rate", "Grüns in Regulation", "Anteil", "round", "higher", 5,
               _from_rounds(lambda r: _rate(r.gir_hits, r.gir_recorded), "shots"),
               group="shots"),
        Metric("scramble_rate", "Scrambling", "Anteil", "round", "higher", 5,
               _from_rounds(lambda r: _rate(r.scrambles, r.scramble_chances), "shots"),
               group="shots"),
        Metric("sg_total_per_hole", "Strokes Gained gesamt pro Loch", "Schläge/Loch", "round",
               "higher", 5, _sg_extract(None),
               "selbst berechnet (computed), Näherungs-Baseline; nur vollständig erfasste Löcher",
               group="shots"),
        *(
            Metric(f"sg_{cat}_per_hole", f"Strokes Gained {cat} pro Loch", "Schläge/Loch",
                   "round", "higher", 5, _sg_extract(cat),
                   "selbst berechnet (computed), Näherungs-Baseline", group="shots")
            for cat in CATEGORIES
        ),
        Metric("carry", "Carry (Range, je Schläger)", "m", "shot", None, 20, _carry,
               "Radar-Quellen getrennt betrachten; Filter `club` angeben", stat="median"),
    )
}  # fmt: skip


def observations(
    con: duckdb.DuckDBPyConnection,
    metric_id: str,
    f: Filters,
    baseline: Baseline,
    ctx: Context | None = None,
) -> list[Observation]:
    obs = sorted(
        METRICS[metric_id].extract(con, f, baseline, ctx or Context()), key=lambda o: o.when
    )
    return obs[-f.last_n :] if f.last_n else obs
