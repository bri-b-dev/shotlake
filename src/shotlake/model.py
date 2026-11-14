"""Canonical-Modell (Phase 1: Range-Schläge). Fehlende Werte sind None, nie geschätzt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ShotRecord:
    source: str
    shot_key: str
    club_raw: str
    club_type: str | None
    measurement_method: str  # radar | camera | sensor | sim | gps
    shot_time_utc: datetime | None = None
    carry_m: float | None = None
    total_m: float | None = None
    carry_side_m: float | None = None  # rechts positiv
    total_side_m: float | None = None
    ball_speed_ms: float | None = None
    club_speed_ms: float | None = None
    smash_factor: float | None = None
    launch_angle_deg: float | None = None
    launch_direction_deg: float | None = None  # rechts positiv
    spin_rpm: float | None = None
    spin_axis_deg: float | None = None
    spin_source: str | None = None  # measured | calculated
    apex_m: float | None = None
    attack_angle_deg: float | None = None
    club_path_deg: float | None = None
    face_to_target_deg: float | None = None
    face_to_path_deg: float | None = None
    dynamic_loft_deg: float | None = None
    spin_loft_deg: float | None = None
    indoor: bool | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_kpa: float | None = None
    altitude_m: float | None = None
    wind_speed_ms: float | None = None
    flags: tuple[str, ...] = ()

    def content_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ParsedSession:
    source: str
    session_key: str
    start_utc: datetime
    kind: str = "range"
    shots: list[ShotRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        return f"{self.source}:{self.session_key}"
