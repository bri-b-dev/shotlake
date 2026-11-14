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
