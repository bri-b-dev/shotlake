"""Garmin-Range-CSV (Export aus Garmin Golf, deutsche Spaltennamen, metrisch).

Zwei Kopfzeilen (Namen, Einheiten). Zeitstempel `MM/DD/YY HH:MM:SS PM` mit 24-h-Stunde und
angehängtem AM/PM (Suffix wird ignoriert); lokale Zeit Europe/Berlin. Der Zeitstempel im
Dateinamen ist der Exportzeitpunkt, die Session-Zeit kommt aus den Zeilen.
Annahme: Spin-Einheit `[1/m.]` ist ungenau beschriftet und bedeutet rpm.
Der Spielername wird nicht übernommen.
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
from .base import Connector, check_header
from .errors import SchemaDriftError

TZ = ZoneInfo("Europe/Berlin")
REQUIRED = frozenset(
    {"Datum", "Schlägerart", "Carry-Distanz", "Gesamtstrecke", "Ballgeschwindigkeit"}
)
KNOWN = frozenset(
    {
        "Datum", "Spieler", "Schlägername", "Schlägerart", "Schl.gsch.", "Anstellwinkel",
        "Schwungbahn", "Schlagfläche", "Schlagflächenstellung", "Ballgeschwindigkeit",
        "Smash Factor", "Abflugwinkel", "Abflugrichtung", "Backspin", "Sidespin", "Drehrate",
        "Drehratentyp", "Drehachse", "Höhe des Scheitelpunkts", "Carry-Distanz",
        "Carry-Abweichungswinkel", "Carry-Abweichungsdistanz", "Gesamtstrecke",
        "Gesamtabweichungswinkel", "Gesamtabweichungsdistanz", "Notiz", "Markierung",
        "Luftdichte", "Temperatur", "Luftdruck", "Relative Luftfeuchtigkeit",
    }
)  # fmt: skip
EXPECTED_UNITS = {"Carry-Distanz": "[m]", "Gesamtstrecke": "[m]", "Ballgeschwindigkeit": "[km/h]"}
_TIME = re.compile(r"^(\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\s*[AP]M)?$", re.IGNORECASE)
_SPIN_SOURCE = {"gemessen": "measured", "berechnet": "calculated"}


def _parse_time(text: str) -> datetime:
    m = _TIME.match(text.strip())
    if not m:
        raise ValueError(f"Zeitformat nicht erkannt: {text!r}")
    mo, d, y, h, mi, s = (int(x) for x in m.groups())
    local = datetime(2000 + y, mo, d, h, mi, s, tzinfo=TZ)
    return local.astimezone(UTC)


def _identity(value: float) -> float:
    return value


def _f(row: dict[str, str], col: str, conv: Callable[[float], float] = _identity) -> float | None:
    text = row.get(col)
    if text is None or text.strip() == "":
        return None
    try:
        return conv(float(text))
    except ValueError:
        return None


def parse_garmin_range_csv(path: Path | str) -> ParsedSession:
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        raise SchemaDriftError("Header- und Einheitenzeile erwartet")
    header, unit_row = rows[0], rows[1]
    report = check_header(header, unit_row, REQUIRED, KNOWN, EXPECTED_UNITS)
    if not report.ok:
        raise SchemaDriftError("; ".join(report.errors))
    index = {name: i for i, name in enumerate(header)}

    shots: list[ShotRecord] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for line_no, cells in enumerate(rows[2:], start=3):
        if not cells or not any(c.strip() for c in cells):
            continue
        row = {name: (cells[i] if i < len(cells) else "") for name, i in index.items()}
        try:
            when = _parse_time(row["Datum"])
        except ValueError as exc:
            errors.append(f"Zeile {line_no}: {exc}")
            continue
        club_raw = row["Schlägerart"].strip()
        n = seen.get((row["Datum"], club_raw), 0)
        seen[(row["Datum"], club_raw)] = n + 1
        club_type = canonical_club(club_raw)
        shots.append(
            ShotRecord(
                source="garmin_range",
                shot_key=f"{when.isoformat()}|{club_raw}|{n}",
                shot_time_utc=when,
                club_raw=club_raw,
                club_type=club_type,
                measurement_method="radar",
                flags=() if club_type else ("club_unmapped",),
                club_speed_ms=_f(row, "Schl.gsch.", units.kmh_to_ms),
                ball_speed_ms=_f(row, "Ballgeschwindigkeit", units.kmh_to_ms),
                smash_factor=_f(row, "Smash Factor"),
                attack_angle_deg=_f(row, "Anstellwinkel"),
                club_path_deg=_f(row, "Schwungbahn"),
                face_to_target_deg=_f(row, "Schlagfläche"),
                face_to_path_deg=_f(row, "Schlagflächenstellung"),
                launch_angle_deg=_f(row, "Abflugwinkel"),
                launch_direction_deg=_f(row, "Abflugrichtung"),
                spin_rpm=_f(row, "Drehrate"),
                spin_source=_SPIN_SOURCE.get(row.get("Drehratentyp", "").strip().lower()),
                spin_axis_deg=_f(row, "Drehachse"),
                apex_m=_f(row, "Höhe des Scheitelpunkts"),
                carry_m=_f(row, "Carry-Distanz"),
                carry_side_m=_f(row, "Carry-Abweichungsdistanz"),
                total_m=_f(row, "Gesamtstrecke"),
                total_side_m=_f(row, "Gesamtabweichungsdistanz"),
                temperature_c=_f(row, "Temperatur"),
                pressure_kpa=_f(row, "Luftdruck"),
                humidity_pct=_f(row, "Relative Luftfeuchtigkeit"),
            )
        )
    if not shots:
        raise SchemaDriftError("Keine Schläge gefunden")
    start = min(s.shot_time_utc for s in shots if s.shot_time_utc)
    local_start = start.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S")
    return ParsedSession(
        source="garmin_range",
        session_key=local_start,
        start_utc=start,
        shots=shots,
        errors=errors,
        warnings=list(report.warnings),
    )


class GarminRangeConnector(Connector):
    id = "garmin_range"
    mode = "file"
    risk = "file_only"
    description = "Garmin Golf Range-Sessions (CSV-Export, deutsche Spaltennamen)"
    required_columns = REQUIRED
    known_columns = KNOWN
    expected_units = EXPECTED_UNITS

    def read_header(self, path: Path) -> tuple[list[str], list[str] | None]:
        with Path(path).open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            return header, next(reader, None)

    def parse(self, path: Path) -> ParsedSession:
        return parse_garmin_range_csv(path)
