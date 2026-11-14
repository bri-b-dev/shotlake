"""Garmin Golf Platzrunden über die inoffizielle Garmin-Connect-API (python-garminconnect).

Pro Runde ein Rohdokument `{summary, details, shot_data}` aus `get_golf_summary`,
`get_golf_scorecard` und `get_golf_shot_data`. Der Client ist hinter `GolfClient` austauschbar
(Tests nutzen Fixtures, kein Netz).

Einheiten (an den echten Daten bestätigt): Positionen von Schlägen/Fahnen sind Semicircles
(Grad = Wert * 180 / 2^31), die Platzkoordinate ist Grad * 1e6, `shotTime` ist Epoch in ms,
`meters` ist Metrik. Putts sind keine Schläge (nur Zähler je Loch).
Cursor: Startzeit der neuesten vollständig geladenen Runde; laufende Runden werden übersprungen.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, ClassVar, Protocol

from ..model import (
    CourseRecord,
    CourseShotRecord,
    HoleRecord,
    ParsedRound,
    TeeRecord,
)
from .api import ApiConnector, AuthError, FetchPage, RateLimitedError, RawRecord, TransientError
from .errors import SchemaDriftError

# Positionen von Schlägen und Fahnen sind Garmin-"Semicircles" (Grad = Wert * 180 / 2^31); die
# Koordinate des Platzes (courseSnapshots) ist dagegen Grad * 1e6. Beides an echten Daten geprüft:
# mit Semicircles stimmt Garmins `meters` überall mit der GPS-Strecke überein (Verhältnis 1,001).
SEMICIRCLE_TO_DEG = 180 / 2**31
COURSE_COORD_SCALE = 1e6
PAGE_SIZE = 20
LIE = {
    "teebox": "tee",
    "fairway": "fairway",
    "rough": "rough",
    "bunker": "sand",
    "green": "green",
}
LIE_UNKNOWN = {"unknown", ""}  # Quelle kennt die Lage nicht -> NULL, kein "other"
FAIRWAY = {"HIT": "hit", "LEFT": "left", "RIGHT": "right"}


class GolfClient(Protocol):
    def get_golf_summary(self, start: int, limit: int) -> dict[str, Any]: ...
    def get_golf_scorecard(self, scorecard_id: int) -> dict[str, Any]: ...
    def get_golf_shot_data(self, scorecard_id: int) -> dict[str, Any]: ...


def _coord(value: object) -> float | None:
    """Schlag-/Fahnenposition (Semicircles) -> Grad."""
    return value * SEMICIRCLE_TO_DEG if isinstance(value, int | float) else None


def _course_coord(value: object) -> float | None:
    """Platzkoordinate (Grad * 1e6) -> Grad."""
    return value / COURSE_COORD_SCALE if isinstance(value, int | float) else None


def _utc(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _need(obj: object, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise SchemaDriftError(f"Antwort ohne {'.'.join(path)}")
        cur = cur[key]
    return cur


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _par_and_index(
    snapshot: dict[str, Any], tee: str | None, htype: str | None
) -> tuple[list[int | None], list[int | None]]:
    pars = [int(c) if c.isdigit() else None for c in str(snapshot.get("holePars", ""))]
    index: list[int | None] = []
    for t in snapshot.get("tees", []):
        if t.get("name") == tee and (htype is None or t.get("handicapType") == htype):
            s = str(t.get("holeHandicaps", ""))
            index = [int(s[i : i + 2]) for i in range(0, len(s) - 1, 2) if s[i : i + 2].isdigit()]
            break
    return pars, index


def parse_round(payload: dict[str, Any]) -> ParsedRound:
    detail = _need(payload, "details", "scorecardDetails")[0]
    card: dict[str, Any] = _need(detail, "scorecard")
    snapshots: list[dict[str, Any]] = _need(payload, "details", "courseSnapshots")
    snap_id = (card.get("courseGlobalId"), card.get("courseSnapshotId"))
    snap = next(
        (s for s in snapshots if (s.get("courseGlobalId"), s.get("courseSnapshotId")) == snap_id),
        snapshots[0] if snapshots else None,
    )
    if snap is None:
        raise SchemaDriftError("Platzdaten (courseSnapshots) fehlen")
    start = _utc(card.get("startTime"))
    if start is None:
        raise SchemaDriftError("scorecard.startTime fehlt")

    tees = tuple(
        TeeRecord(t["name"], t.get("handicapType"), t.get("rating"), t.get("slope"))
        for t in snap.get("tees", [])
        if "name" in t
    )
    course = CourseRecord(
        source="garmin_platform",
        course_key=f"{snap.get('courseGlobalId')}-{snap.get('courseSnapshotId')}",
        name=str(snap.get("name", "")),
        country=snap.get("country"),
        city=snap.get("city"),
        lat=_course_coord(snap.get("lat")),
        lon=_course_coord(snap.get("lon")),
        par_total=_opt_int(snap.get("roundPar")),
        tees=tees,
    )
    result = ParsedRound(
        source="garmin_platform",
        round_key=str(card["id"]),
        start_utc=start,
        end_utc=_utc(card.get("endTime")),
        course=course,
        round_type=card.get("roundType"),
        tee_name=card.get("teeBox"),
        tee_rating=card.get("teeBoxRating"),
        tee_slope=_opt_int(card.get("teeBoxSlope")),
        in_progress=bool(card.get("inProgress")),
        exclude_from_stats=bool(card.get("excludeFromStats")),
    )

    pars, index = _par_and_index(snap, card.get("teeBox"), card.get("handicapType"))
    for h in card.get("holes", []):
        strokes = _opt_int(h.get("strokes"))
        if strokes is None:
            continue
        n = int(h["number"])
        outcome = h.get("fairwayShotOutcome")
        if outcome is not None and outcome not in FAIRWAY:
            result.warnings.append(f"Unbekanntes fairwayShotOutcome {outcome!r}")
        result.holes.append(
            HoleRecord(
                number=n,
                par=pars[n - 1] if n <= len(pars) else None,
                stroke_index=index[n - 1] if n <= len(index) else None,
                strokes=strokes,
                putts=_opt_int(h.get("putts")),
                penalties=_opt_int(h.get("penalties")),
                fairway_outcome=FAIRWAY.get(outcome) if outcome else None,
                pin_lat=_coord(h.get("pinPositionLat")),
                pin_lon=_coord(h.get("pinPositionLon")),
            )
        )

    shot_data = payload.get("shot_data")
    if not isinstance(shot_data, dict):
        raise SchemaDriftError("shot_data fehlt")
    hole_shots = shot_data.get("holeShots")
    if not hole_shots:
        # z. B. Runden ohne Schlagerfassung: Scorecard bleibt gültig, es gibt nur keine Schläge
        result.warnings.append("Keine Schlagdaten (holeShots) für diese Runde")
        hole_shots = []
    club_type = {c["id"]: c.get("clubTypeId") for c in shot_data.get("clubDetails", [])}
    unknown_lies: set[str] = set()

    def lie(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if value.lower() in LIE_UNKNOWN:
            return None
        mapped = LIE.get(value.lower())
        if mapped is None:
            unknown_lies.add(value)
            return "other"
        return mapped

    for hole in hole_shots:
        for s in hole.get("shots", []):
            start_loc, end_loc = s.get("startLoc") or {}, s.get("endLoc") or {}
            ts = s.get("shotTime")
            cid = s.get("clubId")
            result.shots.append(
                CourseShotRecord(
                    hole_number=int(s.get("holeNumber", hole.get("holeNumber"))),
                    shot_order=int(s["shotOrder"]),
                    shot_time_utc=(
                        datetime.fromtimestamp(ts / 1000, UTC) if isinstance(ts, int) else None
                    ),
                    club_raw=None if cid in (None, 0) else f"garmin_clubtype_{club_type.get(cid)}",
                    shot_type=s.get("shotType"),
                    shot_source=s.get("shotSource"),
                    distance_m=s.get("meters"),
                    start_lat=_coord(start_loc.get("lat")),
                    start_lon=_coord(start_loc.get("lon")),
                    start_lie=lie(start_loc.get("lie")),
                    start_lie_source=start_loc.get("lieSource"),
                    end_lat=_coord(end_loc.get("lat")),
                    end_lon=_coord(end_loc.get("lon")),
                    end_lie=lie(end_loc.get("lie")),
                    end_lie_source=end_loc.get("lieSource"),
                    exclude_from_stats=bool(s.get("excludeFromStats")),
                )
            )
    if unknown_lies:
        result.warnings.append(f"Unbekannte Lie-Werte: {sorted(unknown_lies)}")
    return result


@dataclass
class GarminPlatformConnector(ApiConnector):
    client: GolfClient | None = None
    tokenstore: str = os.path.expanduser("~/.garminconnect")

    id: ClassVar[str] = "garmin_platform"
    risk: ClassVar[str] = "unofficial_tos_risk"  # type: ignore[assignment]
    description: ClassVar[str] = "Garmin Golf Platzrunden (inoffiziell, python-garminconnect)"
    required_keys: ClassVar[frozenset[str]] = frozenset({"details", "shot_data"})
    known_keys: ClassVar[frozenset[str]] = frozenset({"summary", "details", "shot_data"})

    def authenticate(self) -> None:
        if self.client is not None:
            return
        try:
            garmin = import_module("garminconnect").Garmin()
            garmin.login(self.tokenstore)
        except ModuleNotFoundError as exc:
            raise AuthError("garminconnect nicht installiert (uv sync --extra garmin)") from exc
        except Exception as exc:
            raise self._map(exc) from exc
        self.client = garmin

    @staticmethod
    def _map(exc: Exception) -> Exception:
        name = type(exc).__name__
        if "TooManyRequests" in name:
            return RateLimitedError(None)
        if "Authentication" in name:
            return AuthError(str(exc)[:200])
        if isinstance(exc, ConnectionError | TimeoutError) or "Connection" in name:
            return TransientError(str(exc)[:200])
        return exc

    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        assert self.client is not None, "authenticate() zuerst aufrufen"
        since = _utc(cursor) if cursor else None
        newest: datetime | None = since
        start = 0
        while True:
            try:
                page = self.client.get_golf_summary(start, PAGE_SIZE)
                rows: list[dict[str, Any]] = _need(page, "scorecardSummaries")
                records: list[RawRecord] = []
                reached_known = False
                for row in rows:
                    when = _utc(row.get("startTime"))
                    if row.get("roundInProgress") or when is None:
                        continue
                    if since is not None and when <= since:
                        reached_known = True
                        continue
                    sid = int(row["id"])
                    records.append(
                        RawRecord(
                            external_id=str(sid),
                            kind="round",
                            payload={
                                "summary": row,
                                "details": self.client.get_golf_scorecard(sid),
                                "shot_data": self.client.get_golf_shot_data(sid),
                            },
                        )
                    )
                    newest = when if newest is None or when > newest else newest
            except Exception as exc:
                raise self._map(exc) from exc
            start += PAGE_SIZE
            last = reached_known or not rows or start >= int(page.get("totalRows", 0))
            # Cursor erst am Ende vorrücken: bei Abbruch wird ab dem alten Stand wiederholt
            next_cursor = newest.isoformat() if last and newest is not None else None
            yield FetchPage(records, next_cursor=next_cursor, done=last)
            if last:
                return

    def stable_payload(self, record: RawRecord) -> dict[str, object]:
        """`clubDetails[].lastModifiedTime` ändert sich bei jedem Abruf und zählt nicht."""
        shot = record.payload.get("shot_data")
        if not isinstance(shot, dict) or "clubDetails" not in shot:
            return record.payload
        clubs = [
            {k: v for k, v in c.items() if k != "lastModifiedTime"} if isinstance(c, dict) else c
            for c in shot["clubDetails"]
        ]
        return {**record.payload, "shot_data": {**shot, "clubDetails": clubs}}

    def parse_record(self, record: RawRecord) -> ParsedRound:
        return parse_round(record.payload)
