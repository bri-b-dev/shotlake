"""BeBrassie (interne Web-API) -> Runden mit Löchern und Schlägen.

Ein Rohdokument je Runde: `{**Listeneintrag, **show-round}` ohne Platzgeometrie, Nutzerdaten und
Gerätekennung. Struktur laut Probe vom 2026-11-14: `playedHoles` (Score je Loch), `courseHoles`
(Par und Lochindex je Geschlecht), `shots` (Start `location`, Ziel `destination`, Schläger,
`shotFrom`/`shotTo` als Lage), `courseTee` (Rating/Slope je Geschlecht), `pinPositions`.

ANNAHMEN (nach dem ersten Import gegen die echten Daten zu prüfen):
- Koordinaten sind GeoJSON, also `[Längengrad, Breitengrad]`.
- `calculatedDistance` ist in Metern.
- Die Wertemengen von `shotFrom`/`shotTo`/`shotType` sind nicht belegt: Unbekanntes wird als
  Warnung gemeldet (Lage `other`), nicht geraten.
- Ob Putts als Schläge vorliegen, ist offen: Putts werden nur gezählt, wenn die Schlagzahl eines
  Lochs zu `numShots` passt; Strafschläge sind nicht belegt (bleiben NULL).
- Die Wertung (Damen/Herren) folgt `proPlayerType` bzw. dem Schlüssel in `strokesGained`
  (z. B. WOMEN18); ohne Angabe gibt es kein Rating (nicht geraten).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from ..clubs import canonical_club
from ..config import default_sops_file
from ..model import CourseRecord, CourseShotRecord, HoleRecord, ParsedRound, TeeRecord
from .api import ApiConnector, AuthError, FetchPage, RawRecord, load_secret
from .errors import SchemaDriftError

LIE = {
    "tee": "tee", "teebox": "tee", "fairway": "fairway", "rough": "rough",
    "bunker": "sand", "sand": "sand", "green": "green",
}  # fmt: skip
SHOT_TYPE = (("tee", "TEE"), ("approach", "APPROACH"), ("putt", "PUTT"), ("chip", "CHIP"))
# Rollende Durchschnitte ändern sich bei jedem neuen Lauf und dürfen den raw-Hash nicht ändern
VOLATILE_KEYS = ("hciAvg10", "firAvg10", "girAvg10", "sgAvg10", "wttAvg10")
DROP_FOR_STORAGE = ("userAgent", "note", "images", "aiReportId")


class BeBrassieClientProtocol(Protocol):
    def list_rounds(self) -> list[dict[str, Any]]: ...
    def show_round(self, round_id: str) -> dict[str, Any] | None: ...


def _utc(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lonlat(geo: object) -> tuple[float, float] | None:
    """GeoJSON-Punkt `{coordinates: [lon, lat]}` -> (lat, lon); leere Koordinaten -> None."""
    if isinstance(geo, dict):
        c = geo.get("coordinates")
        if isinstance(c, list) and len(c) == 2 and all(isinstance(x, int | float) for x in c):
            return float(c[1]), float(c[0])
    return None


def _gender(data: dict[str, Any]) -> str | None:
    """`women` oder `men` aus proPlayerType bzw. den strokesGained-Schlüsseln; sonst None."""
    pro = data.get("proPlayerType")
    keys = [pro] if isinstance(pro, str) else []
    sg = data.get("strokesGained")
    if isinstance(sg, dict) and len(sg) == 1:
        keys += list(sg)
    for key in keys:
        if key.upper().startswith("WOMEN") or key.upper() == "LPGA":
            return "women"
        if key.upper().startswith("MEN") or key.upper() == "PGA":
            return "men"
    return None


def _lie(value: object, unknown: set[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    mapped = LIE.get(re.sub(r"[^a-z]", "", value.lower()))
    if mapped is None:
        unknown.add(value)
        return "other"
    return mapped


def _shot_type(value: object, unknown: set[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    low = value.lower()
    for needle, label in SHOT_TYPE:
        if needle in low:
            return label
    unknown.add(f"shotType:{value}")
    return value.upper()


def parse_round(data: dict[str, Any]) -> ParsedRound:
    if "_id" not in data:
        raise SchemaDriftError("Runde ohne _id")
    start = _utc(data.get("startedAt"))
    course_raw = data.get("course")
    if start is None or not isinstance(course_raw, dict):
        raise SchemaDriftError("startedAt oder course fehlt")
    gender = _gender(data)
    tee_raw = _dict(data.get("courseTee"))
    club = _dict(course_raw.get("golfClub"))
    address = _dict(club.get("address"))
    where = _lonlat(address.get("location"))

    tees: list[TeeRecord] = []
    tee_name = str(tee_raw.get("name") or tee_raw.get("colorName") or "") or None
    if tee_name:
        for sex, label in (("Women", "WOMEN"), ("Men", "MEN")):
            rating, slope = _num(tee_raw.get(f"rating{sex}")), _int(tee_raw.get(f"slope{sex}"))
            if rating is not None and slope is not None:
                tees.append(TeeRecord(tee_name, label, rating, slope))
    rating_key = {"women": "Women", "men": "Men"}.get(gender or "")
    tee_rating = _num(tee_raw.get(f"rating{rating_key}")) if rating_key else None
    tee_slope = _int(tee_raw.get(f"slope{rating_key}")) if rating_key else None

    course = CourseRecord(
        source="bebrassie",
        course_key=str(course_raw.get("_id")),
        name=str(club.get("name") or course_raw.get("name") or ""),
        city=address.get("city"),
        lat=where[0] if where else None,
        lon=where[1] if where else None,
        par_total=_int(tee_raw.get("totalPar")),
        tees=tuple(tees),
    )
    result = ParsedRound(
        source="bebrassie",
        round_key=str(data["_id"]),
        start_utc=start,
        end_utc=_utc(data.get("finishedAt")),
        course=course,
        round_type=data.get("roundType") if isinstance(data.get("roundType"), str) else None,
        tee_name=tee_name,
        tee_rating=tee_rating,
        tee_slope=tee_slope,
    )
    if gender is None and tees:
        result.warnings.append("Wertung (Damen/Herren) unbekannt: kein Rating/Slope übernommen")

    played = data.get("playedHoles")
    if not isinstance(played, list):
        result.warnings.append("nur Listeneintrag ohne Lochdaten (show-round fehlte)")
        return result
    course_holes = {
        h["_id"]: h for h in data.get("courseHoles", []) if isinstance(h, dict) and "_id" in h
    }
    by_number = {
        h["holeNumber"]: h for h in course_holes.values() if isinstance(h.get("holeNumber"), int)
    }
    pins = _dict(data.get("pinPositions"))
    hcp_key = {"women": "handicapWomen", "men": "handicapMan"}.get(gender or "")

    unknown: set[str] = set()
    shots_by_hole: dict[int, list[dict[str, Any]]] = {}
    for s in data.get("shots", []) if isinstance(data.get("shots"), list) else []:
        ch = course_holes.get(s.get("courseHole"))
        if ch is not None:
            shots_by_hole.setdefault(ch["holeNumber"], []).append(s)

    pickups = 0
    for ph in played:
        if not isinstance(ph, dict) or not isinstance(ph.get("holeNumber"), int):
            continue
        number = ph["holeNumber"]
        ch = by_number.get(number, {})
        hole_shots = sorted(shots_by_hole.get(number, []), key=lambda s: _int(s.get("number")) or 0)
        strokes = _int(ph.get("numShots"))
        if ph.get("isPickup"):
            pickups += 1
            strokes = None  # aufgenommen: kein gültiger Lochscore
        starts = [_lie(s.get("shotFrom"), unknown) for s in hole_shots]
        complete = strokes is not None and len(hole_shots) == strokes
        putts = sum(1 for lie in starts if lie == "green") if complete else None
        pin = _lonlat(pins.get(str(ph.get("holeIndex"))) or {}) or _lonlat(ch.get("location"))
        result.holes.append(
            HoleRecord(
                number=number,
                par=_int(ph.get("courseHolePar")),
                stroke_index=_int(ch.get(hcp_key)) if hcp_key else None,
                strokes=strokes,
                putts=putts,
                penalties=None,
                fairway_outcome=None,  # Seite des Fehlschlags nicht belegt
                pin_lat=pin[0] if pin else None,
                pin_lon=pin[1] if pin else None,
            )
        )
        for order, s in enumerate(hole_shots, start=1):
            club_info = _dict(s.get("club"))
            club_raw = club_info.get("clubName") or club_info.get("clubShortcut")
            a, b = _lonlat(s.get("location")), _lonlat(s.get("destination"))
            result.shots.append(
                CourseShotRecord(
                    hole_number=number,
                    shot_order=order,
                    club_raw=str(club_raw) if club_raw else None,
                    club_type=canonical_club(str(club_raw)) if club_raw else None,
                    shot_type=_shot_type(s.get("shotType"), unknown),
                    shot_source="BEBRASSIE",
                    distance_m=_num(s.get("calculatedDistance")),
                    start_lat=a[0] if a else None,
                    start_lon=a[1] if a else None,
                    start_lie=_lie(s.get("shotFrom"), unknown),
                    end_lat=b[0] if b else None,
                    end_lon=b[1] if b else None,
                    end_lie=_lie(s.get("shotTo"), unknown),
                )
            )
    if pickups:
        result.warnings.append(f"{pickups} aufgenommene Löcher ohne Score")
    if unknown:
        result.warnings.append(f"Unbekannte Werte: {sorted(unknown)}")
    return result


@dataclass
class BeBrassieConnector(ApiConnector):
    client: BeBrassieClientProtocol | None = None

    id: ClassVar[str] = "bebrassie"
    risk: ClassVar[str] = "unofficial_tos_risk"  # type: ignore[assignment]
    description: ClassVar[str] = "BeBrassie (inoffizielle Web-API)"
    required_keys: ClassVar[frozenset[str]] = frozenset({"_id", "startedAt", "course"})

    def authenticate(self) -> None:
        if self.client is None:
            from .bebrassie_live import BeBrassieClient

            sops = default_sops_file()
            self.client = BeBrassieClient(
                email=load_secret("bebrassie", "email", sops),
                password=load_secret("bebrassie", "password", sops),
            )
        login = getattr(self.client, "login", None)
        if callable(login):
            login()

    def stable_payload(self, record: RawRecord) -> dict[str, object]:
        return {k: v for k, v in record.payload.items() if k not in VOLATILE_KEYS}

    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        from .bebrassie_live import slim

        if self.client is None:
            raise AuthError("authenticate() zuerst aufrufen")
        since = _utc(cursor) if cursor else None
        newest, records, failed = since, [], 0
        for item in self.client.list_rounds():
            when = _utc(item.get("startedAt"))
            if when is None or (since is not None and when <= since):
                continue
            detail = self.client.show_round(str(item["_id"]))
            if detail is None:
                failed += 1  # nur Listeneintrag: Runde wird beim nächsten Lauf erneut versucht
            doc = slim({**item, **(detail or {})})
            for key in DROP_FOR_STORAGE:
                doc.pop(key, None)
            records.append(RawRecord(str(item["_id"]), doc, "round"))
            if detail is not None:
                newest = when if newest is None or when > newest else newest
        if failed:
            self.notes.append(f"{failed} Runden ohne Detaildaten (show-round fehlgeschlagen)")
        yield FetchPage(records, next_cursor=newest.isoformat() if newest and not failed else None)

    def parse_record(self, record: RawRecord) -> ParsedRound:
        return parse_round(record.payload)
