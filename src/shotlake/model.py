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


@dataclass(frozen=True)
class TeeRecord:
    name: str
    handicap_type: str | None  # MEN | WOMEN (Wertungsgruppe des Ratings)
    rating: float | None  # Course Rating (18 Löcher)
    slope: int | None


@dataclass(frozen=True)
class CourseRecord:
    source: str
    course_key: str  # inkl. Platz-Snapshot, damit Änderungen am Platz nachvollziehbar bleiben
    name: str
    country: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    par_total: int | None = None
    tees: tuple[TeeRecord, ...] = ()

    @property
    def course_id(self) -> str:
        return f"{self.source}|{self.course_key}"


@dataclass(frozen=True)
class HoleRecord:
    number: int
    par: int | None = None
    stroke_index: int | None = None
    strokes: int | None = None  # brutto
    putts: int | None = None
    penalties: int | None = None
    fairway_outcome: str | None = None  # hit | left | right | None (Par 3 oder nicht erfasst)
    pin_lat: float | None = None
    pin_lon: float | None = None


@dataclass(frozen=True)
class CourseShotRecord:
    """Schlag auf dem Platz mit Lage am Start/Ende (Lie nach gemeinsamem Vokabular)."""

    hole_number: int
    shot_order: int
    shot_time_utc: datetime | None = None
    club_raw: str | None = None
    club_type: str | None = None
    shot_type: str | None = None
    shot_source: str | None = None
    distance_m: float | None = None
    start_lat: float | None = None
    start_lon: float | None = None
    start_lie: str | None = None
    start_lie_source: str | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    end_lie: str | None = None
    end_lie_source: str | None = None
    exclude_from_stats: bool = False

    def content_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ParsedRound:
    source: str
    round_key: str
    start_utc: datetime
    course: CourseRecord
    end_utc: datetime | None = None
    round_type: str | None = None  # FRONT_9 | BACK_9 | ALL (Quelle)
    tee_name: str | None = None
    tee_rating: float | None = None
    tee_slope: int | None = None
    in_progress: bool = False
    exclude_from_stats: bool = False
    simulated: bool = False
    holes: list[HoleRecord] = field(default_factory=list)
    shots: list[CourseShotRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def round_id(self) -> str:
        return f"{self.source}|{self.round_key}"

    @property
    def holes_played(self) -> int:
        return sum(1 for h in self.holes if h.strokes is not None)

    @property
    def gross_strokes(self) -> int | None:
        played = [h.strokes for h in self.holes if h.strokes is not None]
        return sum(played) if played else None
