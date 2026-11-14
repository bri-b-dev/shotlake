"""Verlauf des Score Differentials (gleitender Durchschnitt). Kein Handicap-Index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .score import RoundScore, round_half_up


@dataclass(frozen=True)
class TrendPoint:
    round_id: str
    start_utc: datetime
    differential: float
    moving_avg: float | None  # None, bis `window` Differentials vorliegen
    window: int
    relevant: bool


def differential_trend(
    scores: list[RoundScore], window: int = 5, only_relevant: bool = False
) -> list[TrendPoint]:
    """Gleitender Durchschnitt über die letzten `window` Runden mit Differential.

    Runden ohne Differential zählen nicht mit; vor dem Erreichen von `window` Werten bleibt der
    Durchschnitt NULL, statt aus zu wenigen Runden einen Wert vorzutäuschen.
    """
    if window < 1:
        raise ValueError("window muss >= 1 sein")
    rows = sorted((s for s in scores if s.differential is not None), key=lambda s: s.start_utc)
    if only_relevant:
        rows = [s for s in rows if s.relevant_effective]
    out: list[TrendPoint] = []
    for i, s in enumerate(rows):
        assert s.differential is not None
        chunk = [r.differential for r in rows[max(0, i - window + 1) : i + 1]]
        avg = (
            round_half_up(sum(v for v in chunk if v is not None) / window)
            if len(chunk) == window
            else None
        )
        out.append(
            TrendPoint(s.round_id, s.start_utc, s.differential, avg, window, s.relevant_effective)
        )
    return out
