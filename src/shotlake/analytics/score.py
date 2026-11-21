"""Score, Rundenstatistik und Score Differential (offizielle Formel, 9-Loch-Variante).

Kein Handicap-Index wird berechnet. `handicap_index_at_play` ist Eingabe (Nutzer vor Quelle).
Fehlende Eingaben ergeben NULL mit Grund, nie einen geschätzten Wert.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import duckdb

from ..courses import MasterIndex
from ..model import HoleRecord

PCC_MIN, PCC_MAX = -1.0, 3.0


def round_half_up(value: float, digits: int = 1) -> float:
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(round(value, 9))).quantize(q, rounding=ROUND_HALF_UP))


def course_handicap(hi: float, slope: float, rating: float, par: int, holes: int = 18) -> int:
    """Spielvorgabe: `HI * Slope / 113 + (CR - Par)`, bei 9 Löchern mit HI/2 und 9-Loch-Werten."""
    index = hi / 2 if holes == 9 else hi
    return math.floor(index * slope / 113 + (rating - par) + 0.5)


def allocate_strokes(course_hcp: int, stroke_indices: list[int]) -> list[int]:
    """Vorgabeschläge je Loch (Reihenfolge wie Eingabe); Rang über die Lochindizes der gespielten
    Löcher. Negative Spielvorgabe (Plus-Handicap) nimmt Schläge an den leichtesten Löchern zurück.
    """
    n = len(stroke_indices)
    order = sorted(range(n), key=lambda i: stroke_indices[i])
    rank = {hole: r for r, hole in enumerate(order, start=1)}
    base, extra = divmod(abs(course_hcp), n)
    out: list[int] = []
    for i in range(n):
        if course_hcp >= 0:
            out.append(base + (1 if rank[i] <= extra else 0))
        else:
            out.append(-(base + (1 if rank[i] > n - extra else 0)))
    return out


def adjusted_gross(holes: list[HoleRecord], received: list[int]) -> int:
    """Höchstscore je Loch: Netto-Doppelbogey (Par + 2 + erhaltene Schläge)."""
    total = 0
    for h, r in zip(holes, received, strict=True):
        assert h.strokes is not None and h.par is not None
        total += min(h.strokes, h.par + 2 + r)
    return total


def differential_18(ags: float, rating: float, slope: float, pcc: float = 0.0) -> float:
    return round_half_up(113 / slope * (ags - rating - pcc))


def expected_nine(hi: float) -> float:
    """WHS: erwartetes Differential der nicht gespielten 9 Löcher, `0,52 * Index + 1,2`."""
    return round_half_up(0.52 * hi + 1.2)


def suggest_relevant(
    *,
    simulated: bool,
    in_progress: bool,
    holes_played: int,
    has_rating: bool,
    excluded_by_source: bool,
) -> bool:
    return (
        not simulated
        and not in_progress
        and holes_played >= 9
        and has_rating
        and not excluded_by_source
    )


@dataclass(frozen=True)
class RoundScore:
    round_id: str
    round_key: str
    start_utc: datetime
    course_name: str
    holes_played: int
    part: str | None  # all | front9 | back9 | nine (9 Löcher ohne Lochdaten) | None (Teilrunde)
    gross: int | None
    par: int | None
    to_par: int | None
    putts: int | None
    penalties: int | None
    fir_hits: int
    fir_recorded: int
    gir_hits: int
    gir_recorded: int
    scrambles: int
    scramble_chances: int
    hi: float | None
    course_hcp: int | None
    net: int | None
    ags: int | None
    ags_source: str | None  # computed | manual | gross (unbereinigt, ohne Höchstscore)
    pcc: float
    differential: float | None
    extrapolated: bool  # 9-Loch-Runde, auf 18 Löcher hochgerechnet
    reason: str | None  # Grund, falls kein Differential
    relevant: bool | None  # Nutzerentscheidung
    relevant_suggestion: bool  # nur Hinweis: formale Kriterien erfüllt; ändert nichts am Standard
    rating_source: str | None = None  # Quelle, falls Rating/Slope von einer verknüpften Runde kommt
    simulated: bool = False  # Simulator-Runde (nicht mit realen Runden mischen)

    @property
    def relevant_effective(self) -> bool:
        """Handicap-relevant ist nur, was der Nutzer so gesetzt hat; Standard ist nein."""
        return self.relevant is True

    @staticmethod
    def rate(hits: int, recorded: int) -> float | None:
        return hits / recorded if recorded else None


def _part(holes: list[HoleRecord]) -> str | None:
    nums = [h.number for h in holes]
    if len(nums) == 18:
        return "all"
    if len(nums) == 9 and all(n <= 9 for n in nums):
        return "front9"
    if len(nums) == 9 and all(n >= 10 for n in nums):
        return "back9"
    return None


def _total(values: list[int | None]) -> int | None:
    """Summe nur, wenn für alle Löcher ein Wert vorliegt (sonst NULL, nie teilweise)."""
    if not values or any(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


def _stats(played: list[HoleRecord]) -> dict[str, int | None]:
    return {
        "gross": _total([h.strokes for h in played]),
        "par": _total([h.par for h in played]),
        "putts": _total([h.putts for h in played]),
        "penalties": _total([h.penalties for h in played]),
    }


def compute_round(
    row: dict[str, object],
    holes: list[HoleRecord],
    inputs: dict[str, object],
    tee9: dict[str, tuple[float, int]],
) -> RoundScore:
    played = sorted((h for h in holes if h.strokes is not None), key=lambda h: h.number)
    part = _part(played)
    st = _stats(played)
    gross, par = st["gross"], st["par"]
    # Runde ohne Lochdaten (nur Kopfwerte der Quelle): Score und Differential bleiben möglich,
    # Statistik (Putts, FIR, GIR) und Höchstscore nicht
    header_n = _int(row.get("holes_played"))
    header_only = not played and header_n in (9, 18) and _int(row.get("gross_strokes")) is not None
    if header_only:
        part = "all" if header_n == 18 else "nine"
        gross, par = _int(row.get("gross_strokes")), _int(row.get("par_played"))

    fir = [h for h in played if h.par is not None and h.par >= 4 and h.fairway_outcome]
    fir_hits = sum(1 for h in fir if h.fairway_outcome == "hit")
    gir_recorded = gir_hits = chances = scrambles = 0
    for h in played:
        if h.putts is None or h.par is None or h.strokes is None:
            continue
        gir_recorded += 1
        if h.strokes - h.putts <= h.par - 2:
            gir_hits += 1
        else:
            chances += 1
            scrambles += 1 if h.strokes <= h.par else 0

    user_hi = inputs.get("handicap_index_at_play")  # nur manuelle Eingabe, nie aus einer Quelle
    hi = float(user_hi) if isinstance(user_hi, int | float) else None
    pcc_raw = inputs.get("pcc")
    pcc = float(pcc_raw) if isinstance(pcc_raw, int | float) else 0.0
    ags_override = inputs.get("ags_override")

    # Rating/Slope des 18-Loch-Tees; bei 9 Löchern optional 9-Loch-Werte (tee-set), die dann
    # verdoppelt als 18-Loch-Äquivalent dienen. Ohne sie gilt das 18-Loch-Rating des Tees.
    rating = _num(row.get("tee_rating"))
    slope = _int(row.get("tee_slope"))
    if "all" in tee9:  # vom Nutzer hinterlegte 18-Loch-Werte haben Vorrang vor der Quelle
        rating, slope = tee9["all"]
    rating9: tuple[float, int] | None = tee9.get(part) if part in ("front9", "back9") else None
    if rating9 is not None:
        rating, slope = 2 * rating9[0], rating9[1]  # Rückfall ohne Handicap-Index (siehe unten)

    holes_n = header_n if header_only and header_n else len(played)
    nine = part in ("front9", "back9", "nine")
    reason: str | None = None
    ch = ags = None
    ags_source: str | None = None
    diff: float | None = None
    stroke_ok = bool(played) and all(
        h.par is not None and h.stroke_index is not None for h in played
    )

    if part is None:
        reason = f"Teilrunde mit {holes_n} Löchern (nur 9 oder 18 Löcher werten)"
    elif rating is None or slope is None:
        reason = "Rating/Slope fehlen"
    else:
        if hi is not None and par is not None and stroke_ok and (not nine or rating9):
            # Höchstscore braucht Spielvorgabe; bei 9 Löchern nur mit 9-Loch-Rating
            ch = (
                course_handicap(hi, rating9[1], rating9[0], par, 9)
                if nine and rating9
                else course_handicap(hi, slope, rating, par, 18)
            )
            received = allocate_strokes(ch, [h.stroke_index or 0 for h in played])
            ags = adjusted_gross(played, received)
            ags_source = "computed"
        if isinstance(ags_override, int):
            ags, ags_source = ags_override, "manual"
        if ags is None and gross is not None:
            # ohne Handicap-Index kein Höchstscore: unbereinigtes Brutto (für den Verlauf nutzbar)
            ags, ags_source = gross, "gross"
        if ags is None:
            reason = "AGS nicht ermittelbar (Par oder Lochindex fehlen)"
        elif nine and rating9 is not None and hi is not None:
            # WHS: 9-Loch-Differential (9-Loch-Rating/-Slope) plus erwartetes Differential der
            # übrigen 9 Löcher aus dem Handicap-Index vor der Runde
            diff = round_half_up(
                differential_18(ags, rating9[0], rating9[1], pcc) + expected_nine(hi)
            )
        elif nine:
            # ohne Index oder 9-Loch-Rating: doppelter AGS gegen das 18-Loch-Rating (Näherung)
            diff = differential_18(2 * ags, rating, slope, pcc)
        else:
            diff = differential_18(ags, rating, slope, pcc)

    net = (
        gross - sum(allocate_strokes(ch, [h.stroke_index or 0 for h in played]))
        if gross is not None and ch is not None and stroke_ok
        else None
    )

    relevant = inputs.get("handicap_relevant")
    return RoundScore(
        round_id=str(row["round_id"]),
        round_key=str(row["round_key"]),
        start_utc=row["start_utc"],  # type: ignore[arg-type]
        course_name=str(row.get("course_name") or ""),
        holes_played=holes_n,
        part=part,
        gross=gross,
        par=par,
        to_par=gross - par if gross is not None and par is not None else None,
        putts=st["putts"],
        penalties=st["penalties"],
        fir_hits=fir_hits,
        fir_recorded=len(fir),
        gir_hits=gir_hits,
        gir_recorded=gir_recorded,
        scrambles=scrambles,
        scramble_chances=chances,
        hi=hi,
        course_hcp=ch,
        net=net,
        ags=ags,
        ags_source=ags_source,
        pcc=pcc,
        differential=diff,
        extrapolated=nine and diff is not None,
        reason=reason,
        relevant=relevant if isinstance(relevant, bool) else None,
        simulated=bool(row.get("simulated")),
        relevant_suggestion=suggest_relevant(
            simulated=bool(row.get("simulated")),
            in_progress=bool(row.get("in_progress")),
            holes_played=holes_n,
            has_rating=rating is not None and slope is not None,
            excluded_by_source=bool(row.get("exclude_from_stats")),
        ),
    )


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _text(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _int(v: object) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _dicts(cur: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def round_scores(
    con: duckdb.DuckDBPyConnection,
    *,
    only_relevant: bool = False,
    round_ref: str | None = None,
    source: str | None = None,
    exclude: Collection[str] | None = None,
    donors: dict[str, tuple[float, int, str]] | None = None,
) -> list[RoundScore]:
    """Runden mit Score und Differential. `exclude`: nicht führende Dubletten auslassen;
    `donors`: Rating/Slope einer verknüpften Runde für Runden, deren Quelle keines liefert."""
    conditions: list[str] = []
    params: list[object] = []
    if round_ref:
        conditions.append("(r.round_id = ? or r.round_key = ?)")
        params += [round_ref, round_ref]
    if source:
        conditions.append("r.source = ?")
        params.append(source)
    where = "where " + " and ".join(conditions) if conditions else ""
    rounds = _dicts(
        con.execute(
            f"select r.*, c.name as course_name from round r join course c using (course_id) "
            f"{where} order by r.start_utc",
            params,
        )
    )
    # Löcher, Eingaben und Tee-Werte je einmal für alle Runden laden (statt je Runde einzeln)
    holes_by: dict[object, list[HoleRecord]] = defaultdict(list)
    for h in con.execute(
        "select h.round_id, h.hole_number, h.par, h.stroke_index, h.strokes, h.putts, "
        "h.penalties, h.fairway_outcome, h.pin_lat, h.pin_lon "
        f"from hole h join round r on r.round_id = h.round_id {where} "
        "order by h.round_id, h.hole_number",
        params,
    ).fetchall():
        holes_by[h[0]].append(HoleRecord(*h[1:]))
    inputs_by = {
        row["round_id"]: row
        for row in _dicts(
            con.execute(
                f"select i.* from round_input i join round r on r.round_id = i.round_id {where}",
                params,
            )
        )
    }
    tee9_by: dict[tuple[str, str], dict[str, tuple[float, int]]] = defaultdict(dict)
    for course_id, tee_name, part, rating, slope in con.execute(
        "select course_id, tee_name, part, rating, slope from tee_rating"
    ).fetchall():
        tee9_by[(course_id, tee_name)][part] = (rating, slope)
    masters = MasterIndex.load(con)
    out: list[RoundScore] = []
    skip = set(exclude or ())
    for row in rounds:
        rid = row["round_id"]
        if rid in skip:
            continue
        rating_from: str | None = None
        if (
            donors
            and rid in donors
            and (row.get("tee_rating") is None or row.get("tee_slope") is None)
        ):
            row["tee_rating"], row["tee_slope"], rating_from = donors[str(rid)]
        holes = holes_by.get(rid, [])
        inputs = inputs_by.get(rid, {})
        tee9 = dict(tee9_by.get((str(row["course_id"]), str(row["tee_name"] or "")), {}))
        if masters is not None and (row.get("tee_rating") is None or row.get("tee_slope") is None):
            # letzter Rückfall: eigene Stammdaten des Platzes; Eingaben je Runde (tee-set) gehen vor
            found = masters.rating_for(str(row["course_id"]), _text(row.get("tee_name")))
            if found is not None:
                added = [part for part in found.parts if part not in tee9]
                for part in added:
                    tee9[part] = found.parts[part]
                if added:  # nur kennzeichnen, wenn Stammdaten tatsächlich Werte beigesteuert haben
                    rating_from = rating_from or found.label
        score = compute_round(row, holes, inputs, tee9)
        if rating_from:
            score = replace(score, rating_source=rating_from)
        if only_relevant and not score.relevant_effective:
            continue
        out.append(score)
    return out
