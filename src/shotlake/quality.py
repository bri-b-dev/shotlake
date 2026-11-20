"""Plausibilitätsprüfung: markiert Auffälligkeiten, korrigiert oder löscht nie."""

from __future__ import annotations

from dataclasses import replace

from .model import ShotRecord

MAX_CARRY_M = 450.0
MAX_BALL_SPEED_MS = 110.0
EXCLUDE_FLAGS = ("outlier", "inconsistent")


def assess(shot: ShotRecord) -> ShotRecord:
    flags = list(shot.flags)
    carry_bad = shot.carry_m is not None and not (0 <= shot.carry_m <= MAX_CARRY_M)
    speed_bad = shot.ball_speed_ms is not None and shot.ball_speed_ms > MAX_BALL_SPEED_MS
    if carry_bad or speed_bad:
        flags.append("outlier")
    if shot.carry_m is not None and shot.total_m is not None and shot.total_m < shot.carry_m - 0.5:
        flags.append("inconsistent")
    return replace(shot, flags=tuple(dict.fromkeys(flags)))


# Median der Ballgeschwindigkeit je Schläger und Methode, einmal berechnet und per Join an jeden
# Schlag gehängt (ein korrelierter Teilselect je Schlag wüchse quadratisch mit der Schlagzahl).
SPEED_MEDIAN_JOIN = (
    "left join (select club_type, measurement_method, median(ball_speed_ms) as med "
    "from shot where ball_speed_ms is not null group by 1, 2) sm "
    "on sm.club_type = sh.club_type and sm.measurement_method = sh.measurement_method"
)
SPEED_RATIO_CONDITION = "(sh.ball_speed_ms is null or sh.ball_speed_ms >= ? * sm.med)"
