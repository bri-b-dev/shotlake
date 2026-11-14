"""FlightScope Mevo+ CSV (myflightscope.com Export).

Eine Datei = eine Session; Session-ID und Startzeit stammen aus dem Dateinamen
(`18-jul-2026_12_09_pm.csv`, lokale Zeit Europe/Berlin, 12-h mit am/pm).
Pro Schlägerblock stehen Summenzeilen `Avg`/`Dev` (keine Schläge, teils mit weniger Spalten).

Annahme (zu bestätigen): `Lateral (yds)` ist die seitliche Abweichung bei der Landung (Carry).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import units
from ..clubs import canonical_club
from ..model import ParsedSession, ShotRecord
from .errors import SchemaDriftError

TZ = ZoneInfo("Europe/Berlin")
REQUIRED = ("club", "Shot", "Ball (mph)", "Carry (yds)", "Total (yds)")
_FILENAME = re.compile(r"^(\d{2})-([a-z]{3})-(\d{4})_(\d{1,2})_(\d{2})_(am|pm)$", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _session_time(stem: str) -> datetime:
    m = _FILENAME.match(stem)
    if not m:
        raise SchemaDriftError(f"Unbekanntes Dateinamenformat: {stem!r}")
    day, mon, year, hour, minute, ampm = m.groups()
    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    local = datetime(int(year), _MONTHS[mon.lower()], int(day), h, int(minute), tzinfo=TZ)
    return local.astimezone(UTC)


Row = dict[str, str | None]


def _identity(value: float) -> float:
    return value


def _f(row: Row, col: str, conv: Callable[[float], float] = _identity) -> float | None:
    v = units.parse_number(row.get(col))
    return None if v is None else conv(v)


def _s(row: Row, col: str, conv: Callable[[float], float] = _identity) -> float | None:
    v = units.parse_signed(row.get(col))
    return None if v is None else conv(v)


def _indoor(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    return {"indoor": True, "outdoor": False}.get(v)


def parse_mevo_csv(path: Path | str) -> ParsedSession:
    path = Path(path)
    start = _session_time(path.stem)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise SchemaDriftError("Leere Datei")
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            raise SchemaDriftError(f"Pflichtspalten fehlen: {missing}")
        index = {name: i for i, name in enumerate(header)}
        session = ParsedSession(source="mevo", session_key=path.stem, start_utc=start)
        for line_no, cells in enumerate(reader, start=2):
            if not cells or not any(c.strip() for c in cells):
                continue
            shot_no = cells[index["Shot"]].strip() if len(cells) > index["Shot"] else ""
            if shot_no.lower() in {"avg", "dev"}:
                continue
            row: Row = {name: (cells[i] if i < len(cells) else None) for name, i in index.items()}
            if not shot_no.isdigit():
                session.errors.append(f"Zeile {line_no}: unerwartete Schlagnummer {shot_no!r}")
                continue
            session.shots.append(_to_shot(row, shot_no))
    return session


def _to_shot(row: Row, shot_no: str) -> ShotRecord:
    club_raw = (row["club"] or "").strip()
    club_type = canonical_club(club_raw)
    flags = () if club_type else ("club_unmapped",)
    return ShotRecord(
        source="mevo",
        shot_key=f"{club_raw}|{shot_no}",
        club_raw=club_raw,
        club_type=club_type,
        measurement_method="radar",
        flags=flags,
        ball_speed_ms=_f(row, "Ball (mph)", units.mph_to_ms),
        club_speed_ms=_f(row, "Club (mph)", units.mph_to_ms),
        smash_factor=_f(row, "Smash"),
        carry_m=_f(row, "Carry (yds)", units.yd_to_m),
        total_m=_f(row, "Total (yds)", units.yd_to_m),
        carry_side_m=_s(row, "Lateral (yds)", units.yd_to_m),
        spin_rpm=_f(row, "Spin (rpm)"),
        spin_axis_deg=_s(row, "Spin Axis (°)"),
        apex_m=_f(row, "Height (ft)", units.ft_to_m),
        attack_angle_deg=_f(row, "AOA (°)"),
        spin_loft_deg=_f(row, "Spin Loft (°)"),
        dynamic_loft_deg=_f(row, "Dynamic Loft (°)"),
        club_path_deg=_s(row, "Club Path (°)"),
        face_to_target_deg=_s(row, "FTT (°)"),
        face_to_path_deg=_s(row, "FTP (°)"),
        launch_direction_deg=_s(row, "Launch H (°)"),
        launch_angle_deg=_f(row, "Launch V (°)"),
        indoor=_indoor(row.get("Mode")),
        temperature_c=_f(row, "Temperature (°F)", units.f_to_c),
        humidity_pct=_f(row, "Humidity (%)"),
        pressure_kpa=_f(row, "Air Pressure (psi)", lambda v: v * 6.894757),
        altitude_m=_f(row, "Altitude (ft)", units.ft_to_m),
        wind_speed_ms=_f(row, "Wind Speed (mph)", units.mph_to_ms),
    )
