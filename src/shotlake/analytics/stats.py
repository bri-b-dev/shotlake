"""Statistik-Grundlagen: Bootstrap-Konfidenzintervalle und Zeitraumvergleich.

Deterministisch (fester Seed), ohne externe Abhängigkeiten. Aussagen über Richtung („besser" /
„schlechter") gibt es nur ab einer Mindest-Stichprobe und nur, wenn das Intervall der Differenz
die Null nicht einschließt.
"""

from __future__ import annotations

import math
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


# Schnelle Intervalle für Tabellen mit vielen Gruppen (Schläger): ohne Resampling, deshalb auch bei
# tausenden Schlägen sofort da. Median: verteilungsfrei über Rangzahlen (Binomial-Näherung);
# Mittelwert: t-Intervall. Der Bootstrap bleibt für Aussagen über Runden (kleine n, Insights).
MIN_N_FAST = 10
_Z95 = 1.959964
_T975 = {  # t-Quantil 0,975 je Freiheitsgrade; dazwischen gilt der kleinere Eintrag (vorsichtig)
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
    10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000,
    120: 1.980,
}  # fmt: skip


def _t975(df: int) -> float:
    keys = [k for k in _T975 if k <= df]
    return _T975[keys[-1]] if keys and df < 1000 else _Z95


def median_ci(values: Sequence[float]) -> Interval | None:
    """95-%-Intervall des Medians (Rangzahlen); None bei weniger als 10 Werten."""
    n = len(values)
    if n < MIN_N_FAST:
        return None
    ordered = sorted(values)
    half = _Z95 * math.sqrt(n) / 2
    lo_rank = max(1, math.floor(n / 2 - half))
    hi_rank = min(n, math.ceil(1 + n / 2 + half))
    return Interval(ordered[lo_rank - 1], ordered[hi_rank - 1])


def mean_ci(mean: float | None, std: float | None, n: int) -> Interval | None:
    """95-%-Intervall des Mittelwerts (t-Verteilung); None bei n < 10 oder fehlender Streuung."""
    if mean is None or std is None or n < MIN_N_FAST:
        return None
    half = _t975(n - 1) * std / math.sqrt(n)
    return Interval(mean - half, mean + half)


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
