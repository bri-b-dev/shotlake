"""BeBrassie (interne Web-API) -> Runden mit Löchern und Schlägen.

Ein Rohdokument je Runde: `{**Listeneintrag, **show-round}` ohne Platzgeometrie, Nutzerdaten und
Gerätekennung. Struktur laut Probe vom 2026-11-14: `playedHoles` (Score je Loch), `courseHoles`
(Par und Lochindex je Geschlecht), `shots` (Start `location`, Ziel `destination`, Schläger,
`shotFrom`/`shotTo` als Lage), `courseTee` (Rating/Slope je Geschlecht), `pinPositions`.

Aus den echten Daten (18 Runden, 115 Löcher mit Detail) bestätigt:
- Koordinaten sind GeoJSON `[Länge, Breite]`; `calculatedDistance` ist in Metern.
- Putts sind Schläge (`shotType = putt`, Start auf dem Grün); die Zahl der Schläge je Loch
  entspricht immer `numShots`. `number` zählt je Loch ab 0.
- Strafschläge stehen als eigener Schlag (`penalty`). Sie werden nicht als Schlag gespeichert,
  sondern als Strafschlag des Lochs; der Schlag davor endet am Droppunkt (z. B. nach Wasser).
- Die Lage `roughout` kommt als Start und als Ziel vor (man spielt daraus weiter); ihre Bedeutung
  ist offen, sie wird wie `water` zu `other` und macht betroffene Löcher für SG unvollständig.
- Rating/Slope 0 bedeutet „nicht gepflegt" und ist kein Wert.
- Die Wertung (Damen/Herren) folgt `proPlayerType` bzw. dem Schlüssel in `strokesGained`
  (z. B. WOMEN18); ohne Angabe gibt es kein Rating (nicht geraten).
- Fairway: Treffer, wenn der Abschlag auf dem Fairway endet, sonst `miss` (die Seite ist nicht
  belegt, deshalb kein links/rechts).
- Reine Score-Runden liefern nur Kopfwerte (Löcher, Schläge, zu Par); Tee und Rating fehlen dort.
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

# Lagen (aus den echten Daten abgelesen). `water` (Hindernis) und `roughout` (Bedeutung offen)
# haben keine Baseline-Tabelle und werden zu `other`. `hole` ist das Einlochen (nur als Ziel).
LIE = {
    "tee": "tee", "teebox": "tee", "fairway": "fairway", "rough": "rough",
    "bunker": "sand", "sand": "sand", "green": "green", "hole": "hole",
    "water": "other", "roughout": "other",
}  # fmt: skip
SHOT_TYPES = {
    "teeshot": "TEE", "shottogreen": "APPROACH", "chip": "CHIP", "pitch": "PITCH",
    "putt": "PUTT", "transport": "TRANSPORT", "bunker": "BUNKER", "recovery": "RECOVERY",
    "penalty": "PENALTY",
}  # fmt: skip
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


def _positive(value: object) -> float | None:
    """Rating/Slope 0 bedeutet bei BeBrassie „nicht gepflegt" und ist kein Wert."""
    v = _num(value)
    return v if v is not None and v > 0 else None


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
    label = SHOT_TYPES.get(re.sub(r"[^a-z]", "", value.lower()))
    if label is None:
        unknown.add(f"shotType:{value}")
        return value.upper()
    return label


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
            rating, slope = (
                _positive(tee_raw.get(f"rating{sex}")),
                _positive(tee_raw.get(f"slope{sex}")),
            )
            if rating is not None and slope is not None:
                tees.append(TeeRecord(tee_name, label, rating, int(slope)))
    rating_key = {"women": "Women", "men": "Men"}.get(gender or "")
    tee_rating = _positive(tee_raw.get(f"rating{rating_key}")) if rating_key else None
    slope_value = _positive(tee_raw.get(f"slope{rating_key}")) if rating_key else None
    tee_slope = int(slope_value) if slope_value is not None else None
    if tee_rating is None or tee_slope is None:
        tee_rating = tee_slope = None  # nur gemeinsam gültig

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
        # nur Listeneintrag (z. B. reine Score-Runde): Kopfwerte reichen für Score/Differential
        total_holes, total_shots = _int(data.get("totalHoles")), _int(data.get("totalShots"))
        to_par = _int(data.get("totalToPar"))
        if total_holes and total_shots is not None:
            result.holes_total, result.gross_total = total_holes, total_shots
            if to_par is not None:
                result.par_played = total_shots - to_par
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

        # Strafschläge stehen als eigene "Schläge" in der Liste. Für Strokes Gained sind sie ein
        # Strafschlag auf dem Loch; der Schlag davor endet am Droppunkt (Ziel des Strafschlags).
        real: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        penalties = 0
        for s_raw in hole_shots:
            if _shot_type(s_raw.get("shotType"), set()) == "PENALTY" and real:
                real[-1] = (real[-1][0], s_raw)
                penalties += 1
            else:
                real.append((s_raw, None))
        complete = strokes is not None and len(hole_shots) == strokes
        putts = (
            sum(1 for sr, _ in real if _lie(sr.get("shotFrom"), set()) == "green")
            if complete
            else None
        )
        par = _int(ph.get("courseHolePar"))
        outcome = None
        if par is not None and par >= 4:
            tee_end = next(
                (
                    _lie(sr.get("shotTo"), set())
                    for sr, _ in real
                    if _lie(sr.get("shotFrom"), set()) == "tee"
                ),
                None,
            )
            if tee_end == "fairway":
                outcome = "hit"
            elif tee_end is not None and tee_end != "green":
                outcome = "miss"  # Seite des Fehlschlags ist nicht belegt
        pin = _lonlat(pins.get(str(ph.get("holeIndex")))) or _lonlat(ch.get("location"))
        result.holes.append(
            HoleRecord(
                number=number,
                par=par,
                stroke_index=_int(ch.get(hcp_key)) if hcp_key else None,
                strokes=strokes,
                putts=putts,
                penalties=penalties if complete else None,
                fairway_outcome=outcome,
                pin_lat=pin[0] if pin else None,
                pin_lon=pin[1] if pin else None,
            )
        )
        for order, (s_raw, drop) in enumerate(real, start=1):
            club_info = _dict(s_raw.get("club"))
            # `clubShortcut` ist die Position im Bag (D1, I7, 56°), `clubName` nur das Modell
            club_raw = club_info.get("clubShortcut") or club_info.get("clubName")
            a = _lonlat(s_raw.get("location"))
            end_src = drop if drop is not None else s_raw
            b = _lonlat(end_src.get("destination"))
            end_lie = _lie(end_src.get("shotTo"), unknown)
            _lie(s_raw.get("shotTo"), unknown)  # unbekannte Werte des Originalziels melden
            result.shots.append(
                CourseShotRecord(
                    hole_number=number,
                    shot_order=order,
                    club_raw=str(club_raw) if club_raw else None,
                    club_type=canonical_club(str(club_raw)) if club_raw else None,  # sonst club_map
                    shot_type=_shot_type(s_raw.get("shotType"), unknown),
                    shot_source="BEBRASSIE",
                    distance_m=_num(s_raw.get("calculatedDistance")),
                    start_lat=a[0] if a else None,
                    start_lon=a[1] if a else None,
                    start_lie=_lie(s_raw.get("shotFrom"), unknown),
                    end_lat=b[0] if b else None,
                    end_lon=b[1] if b else None,
                    end_lie=end_lie,
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
