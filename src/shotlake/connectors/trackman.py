"""Trackman Golf (inoffizielle GraphQL-Web-API) -> Range-/Bay-Sessions und Sim-Runden.

Zwei Arten von Rohdokumenten (je eines pro Aktivität bzw. Scorecard):
- `{"_kind": "session", "id", "time", "kind", "strokes": [...]}` -> `ParsedSession` (Radarschläge).
- `{"_kind": "round", ...Scorecard}` -> `ParsedRound` mit `simulated=True`.

Einheiten laut Trackman-Doku (live bestätigt): Geschwindigkeiten m/s, Distanzen m, Spin rpm,
Winkel Grad. Seitenabweichungen werden rechts positiv übernommen (Trackman-Konvention, an
echten Daten noch nicht gegengeprüft). Sim-Runden fließen nicht in Score Differential ein;
Strokes Gained dafür braucht Geokoordinaten (`worldLocation`), die bei Platz-Sims fehlen können.
Unbekannte Lagen und Schlägernamen werden gemeldet, nie geraten.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol

from ..clubs import canonical_club
from ..config import default_sops_file
from ..model import (
    CourseRecord,
    CourseShotRecord,
    HoleRecord,
    ParsedRound,
    ParsedSession,
    ShotRecord,
    TeeRecord,
)
from .api import ApiConnector, AuthError, FetchPage, RawRecord
from .errors import SchemaDriftError

SOURCE = "trackman"
LIE = {
    "tee": "tee", "teebox": "tee", "fairway": "fairway", "rough": "rough", "bunker": "sand",
    "sand": "sand", "green": "green", "hole": "hole", "water": "other",
    # aus dem ersten Live-Lauf (2026-11-14): tiefes/halbes Rough als Rough genähert; Vorgrün,
    # Fels, Mulch, Kiefernnadeln haben keine Baseline-Tabelle
    "deeprough": "rough", "semirough": "rough", "holecup": "hole",
    "fringe": "other", "rock": "other", "mulch": "other", "pinestraw": "other",
}  # fmt: skip
_CLUB_ENUM = [
    (re.compile(r"^WOOD(\d+)$"), "wood_{}"),
    (re.compile(r"^HYBRID(\d+)$"), "hybrid_{}"),
    (re.compile(r"^IRON(\d+)$"), "iron_{}"),
]
_CLUB_FIXED = {
    "DRIVER": "driver", "PITCHINGWEDGE": "wedge_pitching", "SANDWEDGE": "wedge_sand",
    "LOBWEDGE": "wedge_lob", "GAPWEDGE": "wedge_gap", "PUTTER": "putter",
}  # fmt: skip
# Zeitstempel je Quelle (Alias in der Abfrage) und Messwerte (Feld -> ShotRecord-Feld)
_MEASURE = {
    "ballSpeed": "ball_speed_ms", "carry": "carry_m", "total": "total_m",
    "carrySide": "carry_side_m", "totalSide": "total_side_m", "launchAngle": "launch_angle_deg",
    "launchDirection": "launch_direction_deg", "maxHeight": "apex_m",
    "spinAxis": "spin_axis_deg", "clubSpeed": "club_speed_ms", "attackAngle": "attack_angle_deg",
    "clubPath": "club_path_deg", "faceAngle": "face_to_target_deg",
    "faceToPath": "face_to_path_deg", "dynamicLoft": "dynamic_loft_deg",
    "smashFactor": "smash_factor", "spinLoft": "spin_loft_deg",
}  # fmt: skip
SESSION_KEYS = frozenset({"_kind", "id", "time", "kind", "strokes"})
ROUND_REQUIRED = frozenset({"_kind", "id", "startedAt", "course", "holes"})


class TrackmanClientProtocol(Protocol):
    def list_activities(self, since: datetime | None) -> list[dict[str, Any]]: ...
    def session_detail(self, activity_id: str) -> dict[str, Any] | None: ...
    def scorecards(self) -> list[dict[str, Any]]: ...


def _utc(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _int(value: object) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def trackman_club(raw: str | None) -> str | None:
    """Trackman-Schlägername -> kanonisch (Enum-Namen wie `IRON7`, sonst gemeinsame Namen)."""
    if not raw:
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if key in _CLUB_FIXED:
        return _CLUB_FIXED[key]
    for pattern, template in _CLUB_ENUM:
        m = pattern.match(key)
        if m:
            return template.format(m.group(1))
    return canonical_club(raw)  # `WEDGE52` u. Ä. bleiben None -> club_map


def _lie(value: object, unknown: set[str]) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "none":
        return None
    mapped = LIE.get(re.sub(r"[^a-z]", "", value.lower()))
    if mapped is None:
        unknown.add(value)
        return "other"
    return mapped


def _latlon(position: object) -> tuple[float, float] | None:
    world = _dict(_dict(position).get("worldLocation"))
    lat, lon = _num(world.get("latitude")), _num(world.get("longitude"))
    return (lat, lon) if lat is not None and lon is not None else None


def parse_session(data: dict[str, Any]) -> ParsedSession:
    start = _utc(data.get("time"))
    strokes = data.get("strokes")
    if "id" not in data or start is None or not isinstance(strokes, list):
        raise SchemaDriftError("Session ohne id, time oder strokes")
    result = ParsedSession(
        source=SOURCE, session_key=str(data["id"]), start_utc=start, kind="range"
    )
    # Simulator-Sessions (Platz-Sim) getrennt von Range-/Bay-Radar, damit Distanzen nicht mischen
    simulator = data.get("kind") == "SIMULATOR"
    method = "sim" if simulator else "radar"
    indoor = True if simulator else None
    unmapped: set[str] = set()
    for i, stroke in enumerate(strokes):
        if not isinstance(stroke, dict):
            result.errors.append(f"Schlag {i}: kein Objekt")
            continue
        club_raw = str(stroke.get("club") or "")
        club_type = trackman_club(club_raw)
        if club_raw and club_type is None:
            unmapped.add(club_raw)
        when = _utc(stroke.get("rangeTime") or stroke.get("strokeTime"))  # fehlt: NULL
        values = {
            field: _num(_dict(stroke.get("measurement")).get(src))
            for src, field in _MEASURE.items()
        }
        spin = _num(_dict(stroke.get("measurement")).get("spinRate"))
        spin = spin if spin is not None else _num(_dict(stroke.get("measurement")).get("ballSpin"))
        flags: tuple[str, ...] = ()
        if not club_type:
            flags = ("club_unmapped",)
        result.shots.append(
            ShotRecord(
                source=SOURCE,
                shot_key=f"{data['id']}|{i}",
                shot_time_utc=when,
                club_raw=club_raw,
                club_type=club_type,
                measurement_method=method,
                spin_rpm=spin,
                spin_source="measured" if spin is not None else None,
                indoor=indoor,
                flags=flags,
                **values,
            )
        )
    if unmapped:
        result.warnings.append(f"Schläger ohne Zuordnung: {sorted(unmapped)}")
    return result


def parse_round(data: dict[str, Any]) -> ParsedRound:
    start = _utc(data.get("startedAt"))
    course_raw = _dict(data.get("course"))
    if "id" not in data or start is None or not course_raw:
        raise SchemaDriftError("Scorecard ohne id, startedAt oder course")
    where = _latlon(course_raw)
    tees = tuple(
        TeeRecord(
            name=str(t.get("name") or ""),
            handicap_type={"FEMALE": "WOMEN", "MALE": "MEN"}.get(str(t.get("gender"))),
            rating=_num(t.get("rating")),
            slope=_int(t.get("slope")),
        )
        for t in data.get("tees") or []
        if isinstance(t, dict)
    )
    name = str(course_raw.get("displayName") or "")
    course = CourseRecord(
        source=SOURCE,
        course_key=name,
        name=name,
        lat=where[0] if where else None,
        lon=where[1] if where else None,
        par_total=_int(data.get("par")),
        tees=tees,
    )
    result = ParsedRound(
        source=SOURCE,
        round_key=str(data["id"]),
        start_utc=start,
        end_utc=_utc(data.get("finishedAt")),
        tee_name=data.get("teeName") if isinstance(data.get("teeName"), str) else None,
        in_progress=data.get("isCompleted") is False,
        simulated=True,
        course=course,
    )
    unknown: set[str] = set()
    for hole in data.get("holes") or []:
        if not isinstance(hole, dict) or _int(hole.get("holeNumber")) is None:
            continue
        number = int(hole["holeNumber"])
        played = hole.get("isPlayed") is not False
        strokes = _int(hole.get("grossScore")) if played else None
        pin = _latlon(hole.get("pinPosition"))
        hole_shots = [s for s in hole.get("shots") or [] if isinstance(s, dict)]
        putts = _int(hole.get("putts")) if strokes else None
        # Putts stehen nicht als Schläge in den Daten. Passt Schlagzahl + Putts zum Score, gibt
        # es keine Strafschläge; sonst bleibt unbekannt, was die Differenz ist (nicht geraten).
        penalties = (
            0 if strokes and putts is not None and len(hole_shots) + putts == strokes else None
        )
        result.holes.append(
            HoleRecord(
                number=number,
                par=_int(hole.get("par")),
                stroke_index=_int(hole.get("strokeIndex")),
                strokes=strokes or None,
                putts=putts,
                penalties=penalties,
                pin_lat=pin[0] if pin else None,
                pin_lon=pin[1] if pin else None,
            )
        )
        shots = [s for s in hole.get("shots") or [] if isinstance(s, dict)]
        for order, s in enumerate(sorted(shots, key=lambda s: _int(s.get("shotNumber")) or 0), 1):
            a, b = _latlon(s.get("launchPosition")), _latlon(s.get("finalPosition"))
            club_raw = s.get("club") if isinstance(s.get("club"), str) else None
            result.shots.append(
                CourseShotRecord(
                    hole_number=number,
                    shot_order=order,
                    shot_time_utc=_utc(s.get("launchTime")),
                    club_raw=club_raw,
                    club_type=trackman_club(club_raw),
                    shot_source="TRACKMAN",
                    distance_m=_num(s.get("total")),
                    start_lat=a[0] if a else None,
                    start_lon=a[1] if a else None,
                    start_lie=_lie(s.get("launchLie"), unknown),
                    end_lat=b[0] if b else None,
                    end_lon=b[1] if b else None,
                    end_lie=_lie(s.get("finalLie"), unknown),
                )
            )
    if not any(h.strokes for h in result.holes):
        result.warnings.append("Scorecard ohne gespielte Löcher")
    if result.shots and all(s.start_lat is None for s in result.shots):
        result.warnings.append("Sim-Runde ohne Geokoordinaten: Strokes Gained nicht berechenbar")
    if unknown:
        result.warnings.append(f"Unbekannte Lagen: {sorted(unknown)}")
    return result


@dataclass
class TrackmanConnector(ApiConnector):
    client: TrackmanClientProtocol | None = None

    id: ClassVar[str] = SOURCE
    risk: ClassVar[str] = "unofficial_tos_risk"  # type: ignore[assignment]
    description: ClassVar[str] = "Trackman Golf (inoffizielle GraphQL-Web-API, Token aus Browser)"
    required_keys: ClassVar[frozenset[str]] = frozenset({"_kind", "id"})

    def authenticate(self) -> None:
        if self.client is None:
            from .trackman_live import TrackmanClient, load_token

            self.client = TrackmanClient(token=load_token(default_sops_file()))
        check = getattr(self.client, "check", None)
        if callable(check):
            check()

    def contract_check(self, record: RawRecord):  # type: ignore[no-untyped-def]
        report = super().contract_check(record)
        kind = record.payload.get("_kind")
        extra: tuple[str, ...] = ()
        if kind == "session" and not set(record.payload) >= SESSION_KEYS:
            extra = (f"Session-Schlüssel fehlen: {sorted(SESSION_KEYS - set(record.payload))}",)
        if kind == "round" and not set(record.payload) >= ROUND_REQUIRED:
            extra = (f"Scorecard-Schlüssel fehlen: {sorted(ROUND_REQUIRED - set(record.payload))}",)
        if kind not in ("session", "round"):
            extra = (f"Unbekannte Dokumentart: {kind!r}",)
        from .base import ContractReport

        return ContractReport(errors=report.errors + extra, warnings=report.warnings)

    def stable_payload(self, record: RawRecord) -> dict[str, object]:
        return {k: v for k, v in record.payload.items() if k != "lastUpdatedAt"}

    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        if self.client is None:
            raise AuthError("authenticate() zuerst aufrufen")
        state = _decode_cursor(cursor)
        # Aktivitäten ab Cursor minus 1 Tag (Sessions können nachträglich Schläge bekommen)
        since = _utc(state.get("range"))
        newest_range, records = since, []
        for item in self.client.list_activities(since - timedelta(days=1) if since else None):
            when = _utc(item.get("time"))
            detail = self.client.session_detail(str(item["id"]))
            if detail is None:
                self.notes.append(f"Aktivität {item.get('kind')} ohne Schlagdaten übersprungen")
                continue
            records.append(
                RawRecord(
                    str(item["id"]),
                    {"_kind": "session", "id": item["id"], "kind": item.get("kind"), **detail},
                    "session",
                )
            )
            if when and (newest_range is None or when > newest_range):
                newest_range = when
        round_since = _utc(state.get("round"))
        oldest_open: datetime | None = None
        newest_done = round_since
        for card in self.client.scorecards():
            when = _utc(card.get("startedAt"))
            if when is None or (round_since is not None and when <= round_since):
                continue
            records.append(RawRecord(str(card["id"]), {"_kind": "round", **card}, "round"))
            if card.get("isCompleted") is False:
                oldest_open = when if oldest_open is None or when < oldest_open else oldest_open
            elif newest_done is None or when > newest_done:
                newest_done = when
        next_round = newest_done
        if oldest_open is not None:  # laufende Runde beim nächsten Mal erneut holen
            held = oldest_open - timedelta(seconds=1)
            next_round = held if next_round is None else min(next_round, held)
            if round_since is not None:
                next_round = max(next_round, round_since)
        yield FetchPage(records, next_cursor=_encode_cursor(newest_range, next_round))

    def parse_record(self, record: RawRecord) -> ParsedSession | ParsedRound:
        if record.payload.get("_kind") == "round":
            return parse_round(record.payload)
        return parse_session(record.payload)


def _encode_cursor(range_: datetime | None, round_: datetime | None) -> str:
    import json

    return json.dumps(
        {
            "range": range_.isoformat() if range_ else None,
            "round": round_.isoformat() if round_ else None,
        }
    )


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    import json

    if not cursor:
        return {}
    try:
        data = json.loads(cursor)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
