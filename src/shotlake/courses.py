"""Eigene Platz-Stammdaten, unabhängig von Runden.

Ein Stammplatz (`course_master`) hat Name, optional Ort und Koordinaten, ein Standard-Tee und je
Tee Rating/Slope für 18 Löcher (`all`) und optional je Neun (`front9`, `back9`). Plätze der Quellen
(`course`) werden ihm zugeordnet: ausdrücklich (`course_master_link`), sonst über den Namen
(auch Zweitnamen) oder über Koordinaten innerhalb von `GEO_RADIUS_M`.

Die Stammdaten sind der letzte Rückfall für Rating/Slope: Werte der Quelle, die Eingaben je
Runde (`tee-set`) und Werte einer verknüpften Runde gehen vor. Das Tee einer Runde wird über den
Namen gefunden (Farben deutsch/englisch/als Hex-Wert gelten als gleich); fehlt die Angabe,
gilt das Standard-Tee, bei genau einem Tee dieses. Ein unbekanntes Tee ergibt kein Rating.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import duckdb

from .geo import haversine_m

PARTS = ("all", "front9", "back9")
GEO_RADIUS_M = 1000.0
RATING_RANGE = {"all": (50.0, 90.0), "front9": (25.0, 45.0), "back9": (25.0, 45.0)}
SLOPE_RANGE = (55, 155)  # Wertebereich nach WHS

# Teefarben: Namen in mehreren Sprachen und als Hex-Wert auf einen Schlüssel abbilden
_COLORS: dict[str, tuple[str, ...]] = {
    "blau": ("blue", "blau", "#0000ff"),
    "gelb": ("yellow", "gelb", "#ffff00"),
    "rot": ("red", "rot", "#ff0000"),
    "weiss": ("white", "weiss", "weiß", "#ffffff"),
    "schwarz": ("black", "schwarz", "#000000"),
    "gruen": ("green", "grün", "gruen", "#008000", "#00ff00"),
    "orange": ("orange", "#ffa500"),
    "lila": ("purple", "lila", "violett", "#800080"),
    "silber": ("silver", "silber", "#c0c0c0"),
    "gold": ("gold", "#ffd700"),
}
_COLOR_OF = {alias: key for key, aliases in _COLORS.items() for alias in aliases}


class CourseError(ValueError):
    """Ungültige Eingabe zu Stammdaten (Meldung auf Deutsch, für die Anzeige gedacht)."""


def norm(text: str) -> str:
    """Namensvergleich: Groß-/Kleinschreibung, Umlaute und Satzzeichen spielen keine Rolle."""
    folded = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return re.sub(r"[^a-z0-9]+", "", folded.replace("ß", "ss"))


def tee_key(name: str | None) -> str:
    """Schlüssel eines Tee-Namens; Farben (Blue, Blau, #0000FF) fallen zusammen."""
    text = (name or "").strip().casefold()
    return _COLOR_OF.get(text) or norm(text)


@dataclass(frozen=True)
class MasterTee:
    name: str
    part: str
    rating: float
    slope: int
    par: int | None = None


@dataclass(frozen=True)
class Master:
    master_id: str
    name: str
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    par_total: int | None = None
    default_tee: str | None = None
    aliases: tuple[str, ...] = ()
    note: str | None = None
    tees: tuple[MasterTee, ...] = field(default_factory=tuple)

    @property
    def tee_names(self) -> list[str]:
        return list(dict.fromkeys(t.name for t in self.tees))


@dataclass(frozen=True)
class MasterRating:
    """Rating-Werte eines Tees aus den Stammdaten (je Teil), mit Herkunft für die Anzeige."""

    label: str
    tee: str
    parts: dict[str, tuple[float, int]]


# --- Eingaben prüfen ------------------------------------------------------------------------


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold().replace("ß", "ss")).strip("-")
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        base = base.replace(src, dst)
    return re.sub(r"[^a-z0-9-]+", "", base) or "platz"


def _check_tee(name: str, part: str, rating: float, slope: int, par: int | None) -> None:
    if not name.strip():
        raise CourseError("Tee-Name fehlt")
    if part not in PARTS:
        raise CourseError(f"Teil {part!r} unbekannt (all, front9 oder back9)")
    low, high = RATING_RANGE[part]
    if not low <= rating <= high:
        raise CourseError(
            f"Course Rating {rating} unplausibel (erwartet {low:.0f} bis {high:.0f}"
            f"{' je Neun' if part != 'all' else ''})"
        )
    if not SLOPE_RANGE[0] <= slope <= SLOPE_RANGE[1]:
        raise CourseError(
            f"Slope {slope} unplausibel (erwartet {SLOPE_RANGE[0]} bis {SLOPE_RANGE[1]})"
        )
    if par is not None and not (27 <= par <= 80 if part == "all" else 13 <= par <= 40):
        raise CourseError(f"Par {par} unplausibel")


def _check_geo(lat: float | None, lon: float | None) -> None:
    if (lat is None) != (lon is None):
        raise CourseError("Breiten- und Längengrad nur zusammen angeben")
    if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise CourseError("Koordinaten außerhalb des gültigen Bereichs")


# --- Lesen und Schreiben --------------------------------------------------------------------


def _master_from(row: tuple, tees: list[MasterTee]) -> Master:  # type: ignore[type-arg]
    return Master(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
        tuple(row[8] or ()), row[9], tuple(tees),
    )  # fmt: skip


_COLUMNS = "master_id, name, city, country, lat, lon, par_total, default_tee, aliases, note"


def list_masters(con: duckdb.DuckDBPyConnection) -> list[Master]:
    tees: dict[str, list[MasterTee]] = {}
    for mid, name, part, rating, slope, par in con.execute(
        "select master_id, tee_name, part, rating, slope, par from course_master_tee "
        "order by master_id, tee_name, case part when 'all' then 0 when 'front9' then 1 else 2 end"
    ).fetchall():
        tees.setdefault(mid, []).append(MasterTee(name, part, rating, slope, par))
    rows = con.execute(f"select {_COLUMNS} from course_master order by lower(name)").fetchall()
    return [_master_from(r, tees.get(r[0], [])) for r in rows]


def get_master(con: duckdb.DuckDBPyConnection, master_id: str) -> Master | None:
    return next((m for m in list_masters(con) if m.master_id == master_id), None)


def save_master(
    con: duckdb.DuckDBPyConnection,
    *,
    name: str,
    master_id: str | None = None,
    city: str | None = None,
    country: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    par_total: int | None = None,
    default_tee: str | None = None,
    aliases: list[str] | None = None,
    note: str | None = None,
) -> str:
    """Stammplatz anlegen (ohne `master_id`) oder ändern; gibt die Kennung zurück."""
    name = name.strip()
    if not name:
        raise CourseError("Platzname fehlt")
    _check_geo(lat, lon)
    if par_total is not None and not 27 <= par_total <= 80:
        raise CourseError(f"Par {par_total} unplausibel")
    if master_id is None:
        base, n = slug(name), 1
        master_id = base
        taken = {m.master_id for m in list_masters(con)}
        while master_id in taken:
            n += 1
            master_id = f"{base}-{n}"
    elif get_master(con, master_id) is None:
        raise CourseError(f"Stammplatz {master_id!r} nicht gefunden")
    extra = [a for a in (_clean(x) for x in aliases or []) if a]
    con.execute(
        "insert or replace into course_master values (?,?,?,?,?,?,?,?,?,?,?)",
        [master_id, name, _clean(city), _clean(country), lat, lon, par_total,
         _clean(default_tee), extra, _clean(note), datetime.now(UTC).replace(tzinfo=None)],
    )  # fmt: skip
    return master_id


def delete_master(con: duckdb.DuckDBPyConnection, master_id: str) -> None:
    if get_master(con, master_id) is None:
        raise CourseError(f"Stammplatz {master_id!r} nicht gefunden")
    con.execute("delete from course_master_tee where master_id = ?", [master_id])
    con.execute("delete from course_master_link where master_id = ?", [master_id])
    con.execute("delete from course_master where master_id = ?", [master_id])


def set_tee(
    con: duckdb.DuckDBPyConnection,
    master_id: str,
    tee_name: str,
    *,
    rating: float,
    slope: int,
    part: str = "all",
    par: int | None = None,
) -> None:
    """Rating/Slope eines Tees (und Teils) setzen oder ersetzen."""
    if get_master(con, master_id) is None:
        raise CourseError(f"Stammplatz {master_id!r} nicht gefunden")
    tee_name = tee_name.strip()
    _check_tee(tee_name, part, rating, slope, par)
    # gleicher Tee in anderer Schreibweise (Blue/Blau): vorhandenen Namen weiterverwenden
    for (existing,) in con.execute(
        "select distinct tee_name from course_master_tee where master_id = ?", [master_id]
    ).fetchall():
        if tee_key(existing) == tee_key(tee_name):
            tee_name = existing
            break
    con.execute(
        "insert or replace into course_master_tee values (?,?,?,?,?,?)",
        [master_id, tee_name, part, rating, slope, par],
    )


def delete_tee(
    con: duckdb.DuckDBPyConnection, master_id: str, tee_name: str, part: str | None = None
) -> int:
    """Tee (alle Teile oder nur `part`) entfernen; gibt die Zahl gelöschter Einträge zurück."""
    where, params = "master_id = ? and tee_name = ?", [master_id, tee_name]
    if part:
        where, params = where + " and part = ?", [*params, part]
    (n,) = con.execute(f"select count(*) from course_master_tee where {where}", params).fetchone()  # type: ignore[misc]
    con.execute(f"delete from course_master_tee where {where}", params)
    master = get_master(con, master_id)
    if master and master.default_tee and master.default_tee not in master.tee_names:
        con.execute("update course_master set default_tee = null where master_id = ?", [master_id])
    return int(n)


def link_course(con: duckdb.DuckDBPyConnection, course_id: str, master_id: str | None) -> None:
    """Platz einer Quelle ausdrücklich einem Stammplatz zuordnen; `None` = keinem zuordnen."""
    if not con.execute("select 1 from course where course_id = ?", [course_id]).fetchone():
        raise CourseError(f"Platz {course_id!r} nicht gefunden")
    if master_id is not None and get_master(con, master_id) is None:
        raise CourseError(f"Stammplatz {master_id!r} nicht gefunden")
    con.execute("insert or replace into course_master_link values (?,?)", [course_id, master_id])


def unlink_course(con: duckdb.DuckDBPyConnection, course_id: str) -> None:
    """Ausdrückliche Zuordnung entfernen (danach gilt wieder die automatische)."""
    con.execute("delete from course_master_link where course_id = ?", [course_id])


def import_from_course(con: duckdb.DuckDBPyConnection, course_id: str) -> str:
    """Stammplatz aus einem Platz der Quellen anlegen (Name, Ort, Koordinaten, Par, Tees).

    Tees mit Rating/Slope der Quelle werden als 18-Loch-Werte übernommen; der neue Stammplatz
    ist dem Platz sofort zugeordnet. Danach lassen sich die Werte frei ändern.
    """
    row = con.execute(
        "select name, city, country, lat, lon, par_total from course where course_id = ?",
        [course_id],
    ).fetchone()
    if row is None:
        raise CourseError(f"Platz {course_id!r} nicht gefunden")
    master_id = save_master(
        con, name=row[0], city=row[1], country=row[2], lat=row[3], lon=row[4], par_total=row[5]
    )
    for tee, rating, slope in con.execute(
        "select tee_name, rating, slope from course_tee where course_id = ? "
        "and rating is not null and slope is not null order by tee_name",
        [course_id],
    ).fetchall():
        try:
            set_tee(con, master_id, tee, rating=rating, slope=slope)
        except CourseError:
            continue  # unplausible Quellwerte nicht übernehmen
    link_course(con, course_id, master_id)
    return master_id


# --- Zuordnung und Rating -------------------------------------------------------------------


class MasterIndex:
    """Zuordnung Platz der Quelle -> Stammplatz und Rating je Tee (einmal je Auswertung laden)."""

    def __init__(self, masters: list[Master], courses: list[tuple], links: dict[str, str | None]):  # type: ignore[type-arg]
        self.masters = {m.master_id: m for m in masters}
        self._links = links
        self._names: dict[str, str] = {}
        for m in masters:
            for label in (m.name, *m.aliases):
                self._names.setdefault(norm(label), m.master_id)
        self._geo = [m for m in masters if m.lat is not None and m.lon is not None]
        self._courses = {c[0]: c for c in courses}
        self._cache: dict[str, tuple[Master, str] | None] = {}

    @classmethod
    def load(cls, con: duckdb.DuckDBPyConnection) -> MasterIndex | None:
        masters = list_masters(con)
        if not masters:
            return None
        courses = con.execute("select course_id, name, lat, lon from course").fetchall()
        links = dict(con.execute("select course_id, master_id from course_master_link").fetchall())
        return cls(masters, courses, links)

    def master_for(self, course_id: str) -> tuple[Master, str] | None:
        """Stammplatz und Art der Zuordnung (`Zuordnung`, `Name`, `Koordinaten`) oder None."""
        if course_id not in self._cache:
            self._cache[course_id] = self._match(course_id)
        return self._cache[course_id]

    def _match(self, course_id: str) -> tuple[Master, str] | None:
        if course_id in self._links:
            target = self._links[course_id]
            master = self.masters.get(target) if target else None
            return (master, "Zuordnung") if master else None  # NULL = ausdrücklich keiner
        course = self._courses.get(course_id)
        if course is None:
            return None
        mid = self._names.get(norm(course[1]))
        if mid:
            return self.masters[mid], "Name"
        if course[2] is not None and course[3] is not None:
            near = [
                (haversine_m(course[2], course[3], m.lat, m.lon), m)  # type: ignore[arg-type]
                for m in self._geo
            ]
            near = [(d, m) for d, m in near if d <= GEO_RADIUS_M]
            if near:
                return min(near, key=lambda t: t[0])[1], "Koordinaten"
        return None

    def choose_tee(self, master: Master, tee_name: str | None) -> str | None:
        """Tee des Stammplatzes für eine Runde: Name, sonst Standard-Tee, sonst das einzige."""
        names = master.tee_names
        if (tee_name or "").strip():
            wanted = tee_key(tee_name)
            return next((n for n in names if tee_key(n) == wanted), None)
        if master.default_tee and master.default_tee in names:
            return master.default_tee
        return names[0] if len(names) == 1 else None

    def rating_for(self, course_id: str, tee_name: str | None) -> MasterRating | None:
        found = self.master_for(course_id)
        if found is None:
            return None
        master = found[0]
        tee = self.choose_tee(master, tee_name)
        if tee is None:
            return None
        parts = {t.part: (t.rating, t.slope) for t in master.tees if t.name == tee}
        return MasterRating(f"Stammdaten {master.name} / {tee}", tee, parts) if parts else None


# --- Übersicht für die Oberfläche -----------------------------------------------------------


@dataclass(frozen=True)
class SourceCourse:
    """Platz einer Quelle mit Stand der Zuordnung und der Ratings seiner Runden."""

    course_id: str
    source: str
    name: str
    rounds: int
    rounds_no_rating: int  # Runden, die auch nach allen Rückfällen kein Rating haben
    tees: dict[str, int]  # Tee-Name der Runden -> Anzahl ("" = ohne Angabe)
    master: Master | None
    how: str | None  # Zuordnung | Name | Koordinaten
    unmatched_tees: tuple[str, ...]  # Tees der Runden, die der Stammplatz nicht kennt


def source_courses(
    con: duckdb.DuckDBPyConnection, donors: dict[str, tuple[float, int, str]] | None = None
) -> list[SourceCourse]:
    """Plätze der Quellen mit Zuordnung; `donors` (Dedup) verbessert die Zählung ohne Rating."""
    from .analytics.score import round_scores  # erst hier: score importiert dieses Modul

    index = MasterIndex.load(con)
    course_of = dict(con.execute("select round_id, course_id from round").fetchall())
    missing: dict[str, int] = {}
    for score in round_scores(con, donors=donors):
        if score.reason == "Rating/Slope fehlen" and not score.simulated:
            cid = course_of.get(score.round_id)
            if cid is not None:
                missing[cid] = missing.get(cid, 0) + 1
    rows = con.execute(
        "select c.course_id, c.source, c.name, count(r.round_id) "
        "from course c left join round r on r.course_id = c.course_id and not r.simulated "
        "where c.source <> 'trackman' or r.round_id is not null "
        "group by c.course_id, c.source, c.name order by lower(c.name), c.source"
    ).fetchall()
    tees: dict[str, dict[str, int]] = {}
    for cid, tee, n in con.execute(
        "select course_id, coalesce(tee_name, ''), count(*) from round where not simulated "
        "group by 1, 2"
    ).fetchall():
        tees.setdefault(cid, {})[tee] = n
    out = []
    for cid, source, name, n in rows:
        n_missing = missing.get(cid, 0)
        found = index.master_for(cid) if index else None
        master, how = found if found else (None, None)
        seen = tees.get(cid, {})
        unmatched = (
            tuple(t for t in seen if t and index and index.choose_tee(master, t) is None)
            if master
            else ()
        )
        out.append(SourceCourse(cid, source, name, n, n_missing, seen, master, how, unmatched))
    return out
