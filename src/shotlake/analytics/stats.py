"""Statistik-Grundlagen: Bootstrap-Konfidenzintervalle und Zeitraumvergleich.

Deterministisch (fester Seed), ohne externe Abhängigkeiten. Aussagen über Richtung („besser" /
„schlechter") gibt es nur ab einer Mindest-Stichprobe und nur, wenn das Intervall der Differenz
die Null nicht einschließt.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

N_BOOT = 2000
SEED = 20261114
LEVEL = 0.95

Stat = Callable[[Sequence[float]], float]
STATS: dict[str, Stat] = {"mean": statistics.fmean, "median": statistics.median}


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    level: float = LEVEL


# Der Bootstrap ist bei festem Seed eine reine Funktion seiner Eingaben und der teuerste Teil der
# Insights. Gleiche Eingaben (z. B. beim erneuten Öffnen der Seite oder bei einem anderen
# Vergleichsniveau, das diese Werte nicht berührt) liefern daher exakt dasselbe Intervall.
_CACHE_SIZE = 1024


def bootstrap_ci(
    values: Sequence[float], stat: str = "mean", level: float = LEVEL, n_boot: int = N_BOOT
) -> Interval | None:
    """Perzentil-Bootstrap; None bei weniger als 3 Werten (zu wenig für ein Intervall)."""
    if len(values) < 3:
        return None
    return _ci(tuple(values), stat, level, n_boot)


@lru_cache(maxsize=_CACHE_SIZE)
def _ci(values: tuple[float, ...], stat: str, level: float, n_boot: int) -> Interval:
    fn = STATS[stat]
    rng = random.Random(SEED)
    n = len(values)
    draws = sorted(fn(rng.choices(values, k=n)) for _ in range(n_boot))
    lo = draws[int((1 - level) / 2 * n_boot)]
    hi = draws[min(n_boot - 1, int((1 + level) / 2 * n_boot))]
    return Interval(lo, hi, level)


def bootstrap_diff_ci(
    a: Sequence[float], b: Sequence[float], stat: str = "mean", level: float = LEVEL,
    n_boot: int = N_BOOT,
) -> Interval | None:  # fmt: skip
    """Intervall der Differenz stat(a) minus stat(b); None, wenn eine Gruppe < 3 Werte hat."""
    if len(a) < 3 or len(b) < 3:
        return None
    return _diff_ci(tuple(a), tuple(b), stat, level, n_boot)


@lru_cache(maxsize=_CACHE_SIZE)
def _diff_ci(
    a: tuple[float, ...], b: tuple[float, ...], stat: str, level: float, n_boot: int
) -> Interval:
    fn = STATS[stat]
    rng = random.Random(SEED)
    na, nb = len(a), len(b)
    diffs = sorted(fn(rng.choices(a, k=na)) - fn(rng.choices(b, k=nb)) for _ in range(n_boot))
    return Interval(
        diffs[int((1 - level) / 2 * n_boot)], diffs[min(n_boot - 1, int((1 + level) / 2 * n_boot))]
    )


@dataclass(frozen=True)
class Comparison:
    delta: float | None
    relative_change: float | None
    ci: Interval | None
    assessment: str  # zu_wenig_daten | kein_belegbarer_unterschied | wahrscheinlich_besser | ...
    warnings: tuple[str, ...]


def compare(
    a: Sequence[float],
    b: Sequence[float],
    *,
    stat: str = "mean",
    better: str | None = "lower",  # lower | higher | None (keine Wertung)
    min_n: int = 5,
) -> Comparison:
    """Vergleich A gegen B (Differenz A minus B)."""
    warnings: list[str] = []
    if not a or not b:
        return Comparison(None, None, None, "zu_wenig_daten", ("insufficient_data",))
    fn = STATS[stat]
    delta = fn(a) - fn(b)
    base = fn(b)
    rel = delta / abs(base) if base else None
    ci = bootstrap_diff_ci(a, b, stat)
    if min(len(a), len(b)) < min_n:
        warnings.append("insufficient_data")
        return Comparison(delta, rel, ci, "zu_wenig_daten", tuple(warnings))
    if ci is None or ci.low <= 0 <= ci.high:
        return Comparison(delta, rel, ci, "kein_belegbarer_unterschied", tuple(warnings))
    if better is None:
        verdict = "unterschied_belegt"
    else:
        improved = (delta < 0) if better == "lower" else (delta > 0)
        verdict = "wahrscheinlich_besser" if improved else "wahrscheinlich_schlechter"
    return Comparison(delta, rel, ci, verdict, tuple(warnings))
