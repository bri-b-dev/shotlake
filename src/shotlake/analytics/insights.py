"""Regelbasierte Insights, wo Schläge verloren gehen. Deterministisch, nichts erfunden.

Jeder Hinweis nennt Effektgröße, Unsicherheit (95-%-Bootstrap-Intervall, n) und einen Verweis auf
die zugrunde liegenden Daten. Schließt das Intervall die Null ein, gilt der Hinweis als
„unsicher" (er wird standardmäßig nicht angezeigt). Es wird nie über Quellen hinweg gemischt, da
ohne Dedup dieselben Runden mehrfach vorkommen können.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

import duckdb

from ..baseline import Baseline
from ..dedup import Scope
from ..quality import EXCLUDE_FLAGS
from .distance import gapping
from .metrics import Context, Filters, Observation, observations, sg_round_observations
from .sg import CATEGORIES, SGResult
from .stats import Interval, bootstrap_ci, bootstrap_diff_ci, compare

HOLES = 18  # Effekte werden auf 18 Löcher hochgerechnet (besser vorstellbar als pro Loch)
MIN_ROUNDS = 5
MIN_COVERAGE = 0.5
BIAS_MIN_M = 4.0  # ab diesem mittleren Seitenversatz (m) ist ein Bias der Rede wert
BIAS_MIN_SHOTS = 15
GAP_MIN_SHOTS = 10
RECENT = 10  # Vergleich: letzte N Runden gegen die N davor

# Teil des Satzes (Subjekt) und ob das Verb im Plural steht ("kosten" / "kostet")
CATEGORY_NAMES = {
    "tee": ("Abschläge", True), "approach": ("Annäherungsschläge", True),
    "short": ("Das kurze Spiel", False), "green_play": ("Annäherung inkl. Putts", False),
    "putting": ("Putten", False), "penalty": ("Strafschläge", True),
}  # fmt: skip
TREND_METRICS = {
    "to_par_per_hole": ("Schläge zu Par", "lower"),
    "putts_per_hole": ("Putts", "lower"),
    "sg_total_per_hole": ("Strokes Gained gesamt", "higher"),
}


@dataclass(frozen=True)
class Insight:
    id: str
    kind: str  # sg_loss | sg_strength | trend | club_bias | gap | data_quality
    status: str  # belegt | unsicher | hinweis
    priority: float
    text: str
    effect: float | None
    unit: str | None
    ci: Interval | None
    n: int | None
    source: str | None
    links: dict[str, Any] = field(default_factory=dict)
    headline: str = ""  # Kurzfassung für die Oberfläche; Zahlen stehen in effect/ci/n/source


def _de(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _ci_text(ci: Interval | None, scale: float = 1.0, digits: int = 1) -> str:
    """Intervall als „von bis" (aufsteigend), auch nach einem Vorzeichenwechsel."""
    if ci is None:
        return "ohne Intervall"
    lo, hi = sorted((ci.low * scale, ci.high * scale))
    return f"95-%-Intervall {_de(lo, digits)} bis {_de(hi, digits)}"


def _scaled(ci: Interval | None, k: float) -> Interval | None:
    return None if ci is None else Interval(ci.low * k, ci.high * k, ci.level)


def _status(ci: Interval | None) -> str:
    if ci is None or ci.low <= 0 <= ci.high:
        return "unsicher"
    return "belegt"


COMBINED = "alle Quellen, dedupliziert"


def _label(source: str | None) -> str:
    return source or COMBINED


def _args(source: str | None, **extra: Any) -> dict[str, Any]:
    return {**extra, **({"source": source} if source else {})}


def _sources(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("select distinct source from round order by 1").fetchall()]


# --- Strokes Gained -------------------------------------------------------------------------------


def sg_insights(
    baseline: Baseline,
    source: str | None,
    res: SGResult,
    sg_obs: dict[str, list[Observation]],
) -> list[Insight]:
    total = len(res.holes)
    if total < 20:
        return []
    coverage = res.complete_holes / total
    if coverage < MIN_COVERAGE:
        return [
            Insight(
                f"data_quality:sg:{source or 'alle'}", "data_quality", "hinweis", 30,
                f"Strokes Gained für {_label(source)} ist nur eingeschränkt belastbar: Nur "
                f"{coverage:.0%} der Löcher ({res.complete_holes} von {total}) sind vollständig "
                "erfasst, deshalb gibt es dazu keine Insights.",
                coverage, "Anteil", None, total, source,
                {"tool": "get_strokes_gained", "args": _args(source)},
                f"Strokes Gained ist nur eingeschränkt belastbar: {coverage:.0%} der Löcher "
                f"({res.complete_holes} von {total}) sind vollständig erfasst, deshalb gibt es "
                "dazu keine Insights.",
            )
        ]  # fmt: skip
    out: list[Insight] = []
    for cat in CATEGORIES:
        values = [o.value for o in sg_obs[cat]]
        if len(values) < MIN_ROUNDS or not any(values):
            continue
        mean = fmean(values)
        ci = bootstrap_ci(values)
        per18, ci18 = mean * HOLES, _scaled(ci, HOLES)
        status = _status(ci)
        link = {"tool": "get_stats", "args": _args(source, metric=f"sg_{cat}_per_hole")}
        name, plural = CATEGORY_NAMES[cat]
        if mean < 0:
            text = (
                f"{name} {'kosten' if plural else 'kostet'} ca. {_de(-per18)} Schläge pro "
                f"18 Löcher (Verlust gegenüber Baseline {baseline.id}, {_ci_text(ci18, -1.0)}, "
                f"n = {len(values)} Runden, {_label(source)})."
            )
            if status == "unsicher":
                text += " Unsicher: Das Intervall schließt 0 ein."
            priority = (100 + 10 * -per18) if status == "belegt" else 10 + 5 * -per18
            out.append(
                Insight(
                    f"sg_loss:{cat}:{source or 'alle'}",
                    "sg_loss",
                    status,
                    priority,
                    text,
                    per18,
                    "Schläge/18 Löcher",
                    ci18,
                    len(values),
                    source,
                    link,
                    f"{name} {'kosten' if plural else 'kostet'} ca. {_de(-per18)} Schläge pro "
                    "18 Löcher.",
                )
            )
        elif status == "belegt":
            text = (
                f"{name} {'bringen' if plural else 'bringt'} ca. {_de(per18)} Schläge pro "
                f"18 Löcher gegenüber Baseline {baseline.id} ({_ci_text(ci18)}, "
                f"n = {len(values)} Runden, {_label(source)})."
            )
            out.append(
                Insight(
                    f"sg_strength:{cat}:{source or 'alle'}",
                    "sg_strength",
                    status,
                    20 + per18,
                    text,
                    per18,
                    "Schläge/18 Löcher",
                    ci18,
                    len(values),
                    source,
                    link,
                    f"{name} {'bringen' if plural else 'bringt'} ca. {_de(per18)} Schläge pro "
                    "18 Löcher.",
                )
            )
    return out


# --- Veränderung ----------------------------------------------------------------------------------


def trend_insights(
    con: duckdb.DuckDBPyConnection,
    baseline: Baseline,
    source: str | None,
    sg_obs: dict[str, list[Observation]],
    ctx: Context | None = None,
) -> list[Insight]:
    ctx = ctx or Context()
    out: list[Insight] = []
    for metric, (label, better) in TREND_METRICS.items():
        obs = (
            sg_obs["total"]
            if metric == "sg_total_per_hole"
            else observations(con, metric, Filters(source=source), baseline, ctx)
        )
        values = [o.value for o in obs]
        recent, before = values[-RECENT:], values[-2 * RECENT : -RECENT]
        if len(recent) < MIN_ROUNDS or len(before) < MIN_ROUNDS:
            continue
        cmp = compare(recent, before, better=better, min_n=MIN_ROUNDS)
        if cmp.assessment not in ("wahrscheinlich_besser", "wahrscheinlich_schlechter") or (
            cmp.delta is None
        ):
            continue
        improved = cmp.assessment == "wahrscheinlich_besser"
        delta18, ci18 = cmp.delta * HOLES, _scaled(cmp.ci, HOLES)
        direction = "besser" if improved else "schlechter"
        text = (
            f"{label} sind in den letzten {len(recent)} Runden wahrscheinlich {direction} als in "
            f"den {len(before)} davor (Differenz {delta18:+.1f}".replace(".", ",")
            + f" Schläge pro 18 Löcher, {_ci_text(ci18)}, {_label(source)})."
        )
        out.append(Insight(
            f"trend:{metric}:{source or 'alle'}", "trend", "belegt", 60 + abs(delta18) * 5, text,
            delta18, "Schläge/18 Löcher", ci18, len(recent) + len(before), source,
            {"tool": "compare_periods",
             "args": _args(source, metrics=[metric], a={"window": len(recent)})},
            f"{label} sind in den letzten {len(recent)} Runden wahrscheinlich {direction} "
            f"als in den {len(before)} davor.",
        ))  # fmt: skip
    return out


# --- Range: Seiten-Bias und Gapping ----------------------------------------------------------


def _flag_clause() -> str:
    flags = ", ".join(f"'{f}'" for f in EXCLUDE_FLAGS)
    return f"not list_has_any(sh.flags, [{flags}])"


def bias_insights(con: duckdb.DuckDBPyConnection) -> list[Insight]:
    rows = con.execute(
        "select sh.source, sh.club_type, list(sh.carry_side_m) from shot sh "
        f"where sh.club_type is not null and sh.carry_side_m is not null and {_flag_clause()} "
        "group by 1, 2 order by 1, 2"
    ).fetchall()
    out: list[Insight] = []
    for source, club, values in rows:
        if len(values) < BIAS_MIN_SHOTS:
            continue
        mean = fmean(values)
        if abs(mean) < BIAS_MIN_M:
            continue
        ci = bootstrap_ci(values)
        status = _status(ci)
        side = "rechts" if mean > 0 else "links"
        text = (
            f"{club}: Treffer landen im Mittel {_de(abs(mean))} m {side} der Ziellinie "
            f"({_ci_text(ci, 1.0 if mean > 0 else -1.0)}, n = {len(values)} Schläge, {source})."
        )
        if status == "unsicher":
            text += " Unsicher: Das Intervall schließt 0 ein."
        out.append(Insight(
            f"club_bias:{club}:{source}", "club_bias", status,
            (50 if status == "belegt" else 8) + abs(mean), text, mean, "m (rechts positiv)", ci,
            len(values), source,
            {"tool": "get_club_gapping", "args": {"source": source}},
            f"{club}: Treffer landen im Mittel {_de(abs(mean))} m {side} der Ziellinie.",
        ))  # fmt: skip
    return out


def gap_insights(con: duckdb.DuckDBPyConnection) -> list[Insight]:
    out: list[Insight] = []
    carries: dict[tuple[str, str, str], list[float]] = {}
    for source, method, club, values in con.execute(
        "select sh.source, sh.measurement_method, sh.club_type, list(sh.carry_m) from shot sh "
        f"where sh.club_type is not null and sh.carry_m is not null and {_flag_clause()} "
        "group by 1, 2, 3"
    ).fetchall():
        carries[(source, method, club)] = values
    for g in gapping(con, min_n=GAP_MIN_SHOTS):
        if g.status == "ok":
            continue
        a = carries.get((g.source, g.method, g.longer_club), [])
        b = carries.get((g.source, g.method, g.shorter_club), [])
        ci = bootstrap_diff_ci(a, b, stat="median")
        small, over, large = 8.0, 4.0, 18.0
        if ci is None:
            status = "unsicher"
        elif g.status == "gap_too_large":
            status = "belegt" if ci.low > large else "unsicher"
        elif g.status == "overlap":
            status = "belegt" if ci.high < over else "unsicher"
        else:
            status = "belegt" if ci.high < small else "unsicher"
        what = {
            "overlap": "überlappen sich praktisch (Lücke unter 4 m)",
            "gap_too_small": "liegen sehr eng beieinander (Lücke unter 8 m)",
            "gap_too_large": "haben eine große Lücke (über 18 m)",
        }[g.status]
        text = (
            f"{g.longer_club} und {g.shorter_club} {what}: Carry-Differenz der Mediane "
            f"{_de(g.gap_m)} m ({_ci_text(ci)}, n = {len(a)} und {len(b)} Schläge, {g.source})."
        )
        if status == "unsicher":
            text += " Unsicher: Das Intervall erreicht die Schwelle nicht eindeutig."
        out.append(Insight(
            f"gap:{g.longer_club}:{g.shorter_club}:{g.source}:{g.method}", "gap", status,
            40 if status == "belegt" else 6, text, g.gap_m, "m", ci, min(len(a), len(b)),
            g.source, {"tool": "get_club_gapping", "args": {"source": g.source}},
            f"{g.longer_club} und {g.shorter_club} {what}.",
        ))  # fmt: skip
    return out


# --- Zusammenführung ------------------------------------------------------------------------------


def generate(
    con: duckdb.DuckDBPyConnection,
    baseline: Baseline,
    *,
    source: str | None = None,
    include_uncertain: bool = False,
    limit: int | None = None,
    scope: Scope | None = None,
) -> list[Insight]:
    """Insights erzeugen. Mit `scope` (Dedup) und ohne Quelle: ein gemeinsamer Bereich über alle
    Quellen mit führender Quelle je Runde; ohne `scope`: je Quelle getrennt (nie gemischt)."""
    sources: list[str | None] = (
        [source] if source else ([None] if scope is not None else list(_sources(con)))
    )
    found: list[Insight] = []
    ctx = Context(scope)
    for s in sources:
        res = ctx.sg(con, baseline, s)
        sg_obs = sg_round_observations(con, Filters(source=s), baseline, res, ctx)
        found += sg_insights(baseline, s, res, sg_obs)
        found += trend_insights(con, baseline, s, sg_obs, ctx)
    range_insights = bias_insights(con) + gap_insights(con)
    found += [i for i in range_insights if source is None or i.source == source]
    if not include_uncertain:
        found = [i for i in found if i.status != "unsicher"]
    found.sort(key=lambda i: (-i.priority, i.id))
    return found[:limit] if limit else found
