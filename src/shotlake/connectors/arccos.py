"""Arccos Golf (neues Dashboard, SvelteKit Remote Functions) -> Runden mit Schlägen inkl. Putts.

Ein Rohdokument je Runde: `{summary, round, analysis_holes, overall}` aus `roundsBundle` (Zeile)
und `roundDetailBundle` (`data.round`, `data.analysis.holes`, `data.analysis.overall`). Persönliche
Teile der Antwort (Identity, Rarity, Verlauf anderer Runden) werden nicht übernommen.

Besonderheiten gegenüber Garmin: Putts sind echte Schläge mit Position (erste Putt-Distanz ist
damit bekannt); Arccos liefert je Schlag eine eigene SG (`source_provided`, andere Baseline) und
nur Tee-Namen ohne Rating/Slope (Differential braucht diese Werte aus anderer Quelle). Distanzen
sind in Yards, Koordinaten in Grad. Die Fairway-Flags gelten nur für Par 4/5.

Der Live-Zugang (Magic-Link-Session, Funktions-Hashes je Build) ist ungeprüft; deshalb gibt es
`ArccosClient` als Schnittstelle und `HarArccosClient` zum Einlesen eines Browser-HAR-Exports.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ..devalue import DevalueError
from ..devalue import parse as devalue_parse
from ..model import CourseRecord, CourseShotRecord, HoleRecord, ParsedRound, TeeRecord
from .api import ApiConnector, AuthError, FetchPage, RawRecord
from .errors import SchemaDriftError

YD = 0.9144
LIES = {"tee", "fairway", "rough", "sand", "green"}
FACET_TYPE = {"driving": "TEE", "approaching": "APPROACH", "short": "CHIP", "putting": "PUTT"}


class ArccosClient(Protocol):
    def rounds(self) -> list[dict[str, Any]]:
        """Rundenliste (`roundsBundle`), neueste zuerst."""
        ...

    def round_detail(self, round_id: int) -> dict[str, Any] | None:
        """`roundDetailBundle`-Daten (`{round, analysis, ...}`) oder None, wenn nicht verfügbar."""
        ...


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


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _yd(value: object) -> float | None:
    v = _num(value)
    return None if v is None else v * YD


def parse_round(payload: dict[str, Any]) -> ParsedRound:
    rnd = _need(payload, "round")
    meta: dict[str, Any] = _need(rnd, "meta")
    holes_raw: list[dict[str, Any]] = _need(rnd, "holes")
    analysis: list[dict[str, Any]] = _need(payload, "analysis_holes")
    if len(analysis) != len(holes_raw):
        raise SchemaDriftError("Löcher in round und analysis passen nicht zusammen")
    start = _utc(meta.get("startTime"))
    if start is None:
        raise SchemaDriftError("round.meta.startTime fehlt")

    first_tees = holes_raw[0].get("tees", []) if holes_raw else []
    course = CourseRecord(
        source="arccos",
        course_key=f"{meta.get('courseId')}-{meta.get('courseVersion')}",
        name=str(meta.get("courseName", "")),
        city=meta.get("courseCity"),
        par_total=meta.get("courseParFull") if isinstance(meta.get("courseParFull"), int) else None,
        tees=tuple(TeeRecord(str(t["name"]), None, None, None) for t in first_tees if "name" in t),
    )
    round_type = meta.get("roundTypeId")
    result = ParsedRound(
        source="arccos",
        round_key=str(meta["roundId"]),
        start_utc=start,
        end_utc=_utc(meta.get("endTime")),
        course=course,
        round_type=str(round_type) if round_type else None,
        in_progress=not bool(meta.get("isEnded")),
        exclude_from_stats=bool(meta.get("excludedFromStats")),
    )
    unknown: set[str] = set()

    def lie(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if value in LIES:
            return value
        unknown.add(value)
        return "other"

    for h, a in zip(holes_raw, analysis, strict=True):
        number = int(h["holeId"])
        par = h.get("par") if isinstance(h.get("par"), int) else None
        shots = h.get("shots", [])
        strokes = a.get("strokes") if isinstance(a.get("strokes"), int) else None
        putts = sum(1 for s in shots if s.get("startTerrain") == "green")
        penalties = sum(int(s.get("noOfPenalties") or 0) for s in shots)
        outcome = None
        if par is not None and par >= 4:  # bei Par 3 sind die Fairway-Flags nicht aussagekräftig
            outcome = (
                "hit"
                if h.get("isFairWay")
                else "left"
                if h.get("isFairWayLeft")
                else "right"
                if h.get("isFairWayRight")
                else None
            )
        if strokes is not None:
            result.holes.append(
                HoleRecord(
                    number=number,
                    par=par,
                    stroke_index=h.get("handicap") if isinstance(h.get("handicap"), int) else None,
                    strokes=strokes,
                    putts=putts if shots else None,
                    penalties=penalties if shots else None,
                    fairway_outcome=outcome,
                    pin_lat=_num(h.get("pinLat")),
                    pin_lon=_num(h.get("pinLng")),
                )
            )
        hole_excluded = bool(h.get("excludedFromStats"))
        for order, s in enumerate(shots, start=1):
            facet = s.get("sgaFacet")
            kind = "RECOVERY" if s.get("isRecovery") else FACET_TYPE.get(str(facet))
            result.shots.append(
                CourseShotRecord(
                    hole_number=number,
                    shot_order=order,
                    shot_time_utc=_utc(s.get("time")),
                    club_raw=f"arccos_clubtype_{s['clubType']}" if "clubType" in s else None,
                    shot_type=kind,
                    shot_source="ARCCOS",
                    distance_m=_yd(s.get("distanceYards")),
                    start_lat=_num(s.get("startLat")),
                    start_lon=_num(s.get("startLng")),
                    start_lie=lie(s.get("startTerrain")),
                    end_lat=_num(s.get("endLat")),
                    end_lon=_num(s.get("endLng")),
                    end_lie=lie(s.get("endTerrain")),
                    exclude_from_stats=hole_excluded or result.exclude_from_stats,
                    source_sg=_num(s.get("sga")),
                    source_sg_facet=str(facet) if facet else None,
                )
            )
    if unknown:
        result.warnings.append(f"Unbekannte Terrain-Werte: {sorted(unknown)}")
    return result


# --- Client auf Basis eines Browser-HAR-Exports -------------------------------------------------


def _remote_function(url: str) -> str | None:
    if "/_app/remote/" not in url:
        return None
    return url.split("?")[0].rsplit("/", 1)[-1]


@dataclass
class HarArccosClient:
    """Liest `roundsBundle`/`roundDetailBundle`-Antworten aus einem HAR-Export des Dashboards.

    Andere HAR-Einträge werden nicht ausgewertet (sie können Zugangsdaten enthalten). Der Export
    enthält nur die Runden, die im Browser geöffnet wurden; fehlende Details ergeben None.
    """

    path: Path

    def __post_init__(self) -> None:
        self._rows: dict[int, dict[str, Any]] = {}
        self._details: dict[int, dict[str, Any]] = {}
        with Path(self.path).open(encoding="utf-8") as fh:
            har = json.load(fh)
        for entry in har.get("log", {}).get("entries", []):
            name = _remote_function(entry.get("request", {}).get("url", ""))
            if name not in ("roundsBundle", "roundDetailBundle"):
                continue
            text = entry.get("response", {}).get("content", {}).get("text")
            if not text:
                continue
            try:
                outer = json.loads(text)
                if outer.get("type") != "result":
                    continue
                result = devalue_parse(outer["data"])["_"]
            except (KeyError, TypeError, ValueError, DevalueError):
                continue
            if name == "roundsBundle":
                for row in result.get("list", {}).get("data", {}).get("rounds", []):
                    self._rows[int(row["roundId"])] = row
            else:
                data = result.get("bundle", {}).get("data", {})
                meta = data.get("round", {}).get("meta", {})
                if "roundId" in meta:
                    self._details[int(meta["roundId"])] = data

    def rounds(self) -> list[dict[str, Any]]:
        return sorted(self._rows.values(), key=lambda r: str(r.get("startTime")), reverse=True)

    def round_detail(self, round_id: int) -> dict[str, Any] | None:
        return self._details.get(round_id)


def decode_payload_argument(payload: str) -> Any:
    """Argument-Payload einer Remote-Function-URL (base64url, devalue) für Diagnosezwecke."""
    padded = payload + "=" * (-len(payload) % 4)
    return devalue_parse(base64.urlsafe_b64decode(padded).decode())


@dataclass
class ArccosConnector(ApiConnector):
    client: ArccosClient | None = None

    id: ClassVar[str] = "arccos"
    risk: ClassVar[str] = "unofficial_tos_risk"  # type: ignore[assignment]
    description: ClassVar[str] = "Arccos Golf (inoffizielles Dashboard; HAR-Import oder Live)"
    required_keys: ClassVar[frozenset[str]] = frozenset({"round", "analysis_holes"})
    known_keys: ClassVar[frozenset[str]] = frozenset(
        {"summary", "round", "analysis_holes", "overall"}
    )

    def authenticate(self) -> None:
        if self.client is None:
            raise AuthError("Kein Arccos-Zugang: HAR-Export angeben (sync arccos --har DATEI)")

    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        assert self.client is not None, "authenticate() zuerst aufrufen"
        since = _utc(cursor) if cursor else None
        newest = since
        records: list[RawRecord] = []
        skipped = 0
        for row in self.client.rounds():
            when = _utc(row.get("startTime"))
            if not row.get("ended") or when is None or (since is not None and when <= since):
                continue
            detail = self.client.round_detail(int(row["roundId"]))
            if detail is None:
                skipped += 1
                continue
            analysis = detail.get("analysis", {})
            records.append(
                RawRecord(
                    external_id=str(row["roundId"]),
                    kind="round",
                    payload={
                        "summary": row,
                        "round": detail.get("round"),
                        "analysis_holes": analysis.get("holes"),
                        "overall": analysis.get("overall"),
                    },
                )
            )
            newest = when if newest is None or when > newest else newest
        if skipped:
            self.notes.append(f"{skipped} Runden ohne Detaildaten übersprungen (nicht im Export)")
        # Cursor nur vorrücken, wenn nichts übersprungen wurde: sonst gingen Runden verloren
        next_cursor = newest.isoformat() if newest is not None and not skipped else None
        yield FetchPage(records, next_cursor=next_cursor, done=True)

    def parse_record(self, record: RawRecord) -> ParsedRound:
        return parse_round(record.payload)
