"""Dedup: dieselbe Runde in mehreren Quellen erkennen, verknüpfen und führende Quelle wählen.

Regeln (Details in `config/dedup.toml`): gleicher Club (Name/Alias oder Koordinaten), Startzeit
innerhalb des Abstands, Lochzahl und Score. Automatisch verknüpft wird nur bei eindeutigem
Treffer mit gleicher Lochzahl und gleichem Score; alles andere bleibt `vorgeschlagen`.
Entscheidungen des Nutzers (bestätigt, abgelehnt) werden gespeichert und von späteren Läufen
nie überschrieben. Nicht-führende Quellen bleiben vollständig erhalten; nur Auswertungen
wählen je Runde eine Quelle.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb

from .geo import haversine_m

GROUPS = ("score", "shots", "rating")
LINKED = ("auto", "confirmed")


def default_dedup_config_path() -> Path:
    return Path(os.environ.get("SHOTLAKE_DEDUP_CONFIG", "config/dedup.toml"))


@dataclass(frozen=True)
class DedupConfig:
    max_minutes: float = 90.0
    max_hole_difference: int = 1
    auto_score_tolerance: int = 0
    proposal_score_tolerance: int = 10
    geo_radius_m: float = 2000.0
    priority: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "score": ("bebrassie", "garmin_platform", "arccos"),
            "shots": ("arccos", "bebrassie", "garmin_platform"),
            "rating": ("bebrassie", "garmin_platform", "arccos"),
        }
    )
    clubs: dict[str, str] = field(
        default_factory=dict
    )  # normalisierter Name/Alias -> Club-Schlüssel

    def rank(self, group: str, source: str) -> int:
        order = self.priority.get(group, ())
        return order.index(source) if source in order else len(order)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def load_config(path: Path | None) -> DedupConfig:
    if path is None or not path.exists():
        return DedupConfig()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    s = raw.get("settings", {})
    base = DedupConfig()
    priority = dict(base.priority)
    for group, order in raw.get("priority", {}).items():
        if group not in GROUPS:
            raise ValueError(f"{path}: unbekannte Feldgruppe {group!r} (erlaubt: {GROUPS})")
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            raise ValueError(f"{path}: priority.{group} erwartet eine Liste von Quellen")
        priority[group] = tuple(order)
    clubs: dict[str, str] = {}
    for club in raw.get("club", []):
        name = club.get("name")
        if not name:
            raise ValueError(f"{path}: [[club]] ohne name")
        for label in [name, *club.get("aliases", [])]:
            key = _norm(str(label))
            if clubs.get(key, _norm(name)) != _norm(name):
                raise ValueError(f"{path}: Alias {label!r} gehört zu mehreren Clubs")
            clubs[key] = _norm(name)
    return DedupConfig(
        max_minutes=float(s.get("max_minutes", base.max_minutes)),
        max_hole_difference=int(s.get("max_hole_difference", base.max_hole_difference)),
        auto_score_tolerance=int(s.get("auto_score_tolerance", base.auto_score_tolerance)),
        proposal_score_tolerance=int(
            s.get("proposal_score_tolerance", base.proposal_score_tolerance)
        ),
        geo_radius_m=float(s.get("geo_radius_m", base.geo_radius_m)),
        priority=priority,
        clubs=clubs,
    )


@dataclass(frozen=True)
class RoundFacts:
    round_id: str
    source: str
    start: datetime
    holes: int
    gross: int | None
    course_name: str
    lat: float | None
    lon: float | None
    has_shots: bool
    has_rating: bool


def load_facts(con: duckdb.DuckDBPyConnection) -> dict[str, RoundFacts]:
    rows = con.execute(
        """select r.round_id, r.source, r.start_utc, r.holes_played, r.gross_strokes, c.name,
                  c.lat, c.lon,
                  exists (select 1 from course_shot s where s.round_id = r.round_id),
                  r.tee_rating is not null and r.tee_slope is not null
           from round r join course c using (course_id)
           where not r.in_progress and not r.simulated"""
    ).fetchall()
    return {r[0]: RoundFacts(*r) for r in rows}


def club_key(facts: RoundFacts, cfg: DedupConfig) -> str:
    key = _norm(facts.course_name)
    return cfg.clubs.get(key, key)


def same_club(a: RoundFacts, b: RoundFacts, cfg: DedupConfig) -> bool:
    if club_key(a, cfg) == club_key(b, cfg):
        return True
    if None not in (a.lat, a.lon, b.lat, b.lon):
        assert a.lat is not None and a.lon is not None and b.lat is not None and b.lon is not None
        return haversine_m(a.lat, a.lon, b.lat, b.lon) <= cfg.geo_radius_m
    return False


@dataclass(frozen=True)
class Candidate:
    a: str
    b: str  # a < b
    minutes: float
    score_delta: int | None
    hole_diff: int


def pair_key(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


def candidate(a: RoundFacts, b: RoundFacts, cfg: DedupConfig) -> Candidate | None:
    if a.source == b.source or a.holes == 0 or b.holes == 0 or not same_club(a, b, cfg):
        return None
    minutes = abs((a.start - b.start).total_seconds()) / 60
    hole_diff = abs(a.holes - b.holes)
    if minutes > cfg.max_minutes or hole_diff > cfg.max_hole_difference:
        return None
    delta = abs(a.gross - b.gross) if a.gross is not None and b.gross is not None else None
    if hole_diff == 0 and delta is not None and delta > cfg.proposal_score_tolerance:
        return None
    x, y = pair_key(a.round_id, b.round_id)
    return Candidate(x, y, minutes, delta, hole_diff)


@dataclass(frozen=True)
class RunReport:
    auto: int
    proposed: int
    kept_user_decisions: int
    rounds: int

    def as_text(self) -> str:
        return (
            f"Dedup: {self.rounds} Runden; automatisch verknüpft {self.auto}, vorgeschlagen "
            f"{self.proposed}, Nutzerentscheidungen erhalten {self.kept_user_decisions}"
        )


def run(con: duckdb.DuckDBPyConnection, cfg: DedupConfig) -> RunReport:
    """Treffer neu berechnen. Nutzerentscheidungen bleiben unverändert."""
    facts = load_facts(con)
    user = {
        (r[0], r[1]): r[2]
        for r in con.execute(
            "select round_a, round_b, status from round_match where decided_by = 'user'"
        ).fetchall()
    }
    confirmed_partner: dict[tuple[str, str], str] = {}  # (Runde, Quelle des Partners) -> Partner
    for (a, b), status in user.items():
        if status == "confirmed" and a in facts and b in facts:
            confirmed_partner[(a, facts[b].source)] = b
            confirmed_partner[(b, facts[a].source)] = a

    by_pair: dict[tuple[str, str], Candidate] = {}
    ids = sorted(facts)
    for i, x in enumerate(ids):
        for y in ids[i + 1 :]:
            cand = candidate(facts[x], facts[y], cfg)
            if cand is None or (cand.a, cand.b) in user:
                continue
            fa, fb = facts[cand.a], facts[cand.b]
            if confirmed_partner.get((cand.a, fb.source), cand.b) != cand.b:
                continue  # `a` hat in der Quelle von `b` schon einen bestätigten Partner
            if confirmed_partner.get((cand.b, fa.source), cand.a) != cand.a:
                continue
            by_pair[(cand.a, cand.b)] = cand

    # Eindeutigkeit je (Runde, andere Quelle)
    partners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cand in by_pair.values():
        partners[(cand.a, facts[cand.b].source)].add(cand.b)
        partners[(cand.b, facts[cand.a].source)].add(cand.a)

    con.execute("delete from round_match where decided_by = 'auto'")
    auto = proposed = 0
    for cand in by_pair.values():
        fa, fb = facts[cand.a], facts[cand.b]
        unique = len(partners[(cand.a, fb.source)]) == 1 and len(partners[(cand.b, fa.source)]) == 1
        exact = (
            cand.hole_diff == 0
            and cand.score_delta is not None
            and cand.score_delta <= cfg.auto_score_tolerance
        )
        if unique and exact:
            status, confidence, rule = "auto", "hoch", "gleicher_club_zeit_loecher_score"
            auto += 1
        else:
            status, proposed = "proposed", proposed + 1
            if not unique:
                confidence, rule = "niedrig", "mehrdeutig"
            elif cand.hole_diff:
                confidence, rule = "niedrig", "lochzahl_weicht_ab"
            elif cand.score_delta is None:
                confidence, rule = "mittel", "score_unbekannt"
            else:
                confidence, rule = "mittel", "score_weicht_ab"
        con.execute(
            "insert into round_match values (?,?,?,?,?,?,?,?, 'auto', now()::timestamp)",
            [cand.a, cand.b, status, confidence, rule, cand.minutes, cand.score_delta,
             cand.hole_diff],
        )  # fmt: skip
    return RunReport(auto, proposed, len(user), len(facts))


# --- Cluster und führende Quelle ---


def _find(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def clusters(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Gruppen verknüpfter Runden (nur `auto`/`confirmed`), je Gruppe mindestens 2 Runden."""
    rows = con.execute(
        "select round_a, round_b from round_match where status in ('auto', 'confirmed')"
    ).fetchall()
    parent: dict[str, str] = {}
    for a, b in rows:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[_find(parent, a)] = _find(parent, b)
    groups: dict[str, list[str]] = defaultdict(list)
    for x in parent:
        groups[_find(parent, x)].append(x)
    return sorted(sorted(g) for g in groups.values())


def has_data(f: RoundFacts, group: str) -> bool:
    return {"score": f.gross is not None, "shots": f.has_shots, "rating": f.has_rating}[group]


def leader(members: list[RoundFacts], group: str, cfg: DedupConfig) -> RoundFacts:
    return sorted(
        members, key=lambda f: (not has_data(f, group), cfg.rank(group, f.source), f.round_id)
    )[0]


def _excluded(
    facts: dict[str, RoundFacts], groups_of_rounds: list[list[str]], cfg: DedupConfig, group: str
) -> set[str]:
    out: set[str] = set()
    for members in groups_of_rounds:
        present = [facts[m] for m in members if m in facts]
        if len(present) < 2:
            continue
        lead = leader(present, group, cfg)
        out |= {f.round_id for f in present if f.round_id != lead.round_id}
    return out


def excluded_rounds(con: duckdb.DuckDBPyConnection, cfg: DedupConfig, group: str) -> set[str]:
    """Runden, die in ihrer Gruppe nicht führend sind (Auswertungen lassen sie aus)."""
    if group not in GROUPS:
        raise ValueError(f"unbekannte Feldgruppe {group!r}")
    return _excluded(load_facts(con), clusters(con), cfg, group)


def rating_donor(
    con: duckdb.DuckDBPyConnection,
    cfg: DedupConfig,
    groups_of_rounds: list[list[str]] | None = None,
) -> dict[str, tuple[float, int, str]]:
    """Für Runden ohne Rating: Rating/Slope der verknüpften Runde mit der höchsten Priorität."""
    donors: dict[str, tuple[float, int, str]] = {}
    ratings = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute(
            "select round_id, tee_rating, tee_slope, source from round "
            "where tee_rating is not null and tee_slope is not null"
        ).fetchall()
    }
    sources = {r[0]: r[1] for r in con.execute("select round_id, source from round").fetchall()}
    for members in clusters(con) if groups_of_rounds is None else groups_of_rounds:
        candidates = sorted(
            (m for m in members if m in ratings), key=lambda m: cfg.rank("rating", sources[m])
        )
        if not candidates:
            continue
        best = ratings[candidates[0]]
        for m in members:
            if m not in ratings:
                donors[m] = best
    return donors


@dataclass(frozen=True)
class Scope:
    """Dedup-Sicht für Auswertungen: nicht führende Runden je Feldgruppe und Rating-Spender."""

    excluded: dict[str, frozenset[str]] = field(
        default_factory=lambda: {g: frozenset() for g in GROUPS}
    )
    donors: dict[str, tuple[float, int, str]] = field(default_factory=dict)

    def exclude(self, group: str) -> frozenset[str]:
        return self.excluded.get(group, frozenset())

    @classmethod
    def build(cls, con: duckdb.DuckDBPyConnection, cfg: DedupConfig) -> Scope:
        facts = load_facts(con)  # je einmal laden, nicht je Feldgruppe
        linked = clusters(con)
        return cls(
            {g: frozenset(_excluded(facts, linked, cfg, g)) for g in GROUPS},
            rating_donor(con, cfg, linked),
        )


def open_proposals(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("select count(*) from round_match where status = 'proposed'").fetchone()
    return int(row[0]) if row else 0


# --- Nutzerentscheidungen ---


def resolve(con: duckdb.DuckDBPyConnection, ref: str) -> str:
    rows = con.execute(
        "select round_id from round where round_id = ? or round_key = ?", [ref, ref]
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"Runde {ref!r}: {len(rows)} Treffer (Runden-ID oder eindeutigen Schlüssel)"
        )
    return str(rows[0][0])


def _upsert_user(con: duckdb.DuckDBPyConnection, a: str, b: str, status: str, rule: str) -> None:
    x, y = pair_key(a, b)
    existing = con.execute(
        "select 1 from round_match where round_a = ? and round_b = ?", [x, y]
    ).fetchone()
    if existing:
        con.execute(
            "update round_match set status = ?, decided_by = 'user', decided_at = now()::timestamp "
            "where round_a = ? and round_b = ?",
            [status, x, y],
        )
    else:
        facts = load_facts(con)
        fa, fb = facts.get(x), facts.get(y)
        minutes = abs((fa.start - fb.start).total_seconds()) / 60 if fa and fb else None
        delta = (
            fa.gross - fb.gross
            if fa and fb and fa.gross is not None and fb.gross is not None
            else None
        )
        con.execute(
            "insert into round_match values (?,?,?, 'nutzer', ?, ?, ?, ?, 'user', "
            "now()::timestamp)",
            [x, y, status, rule, minutes, abs(delta) if delta is not None else None,
             abs(fa.holes - fb.holes) if fa and fb else None],
        )  # fmt: skip


def _cluster_of(con: duckdb.DuckDBPyConnection, round_id: str) -> list[str]:
    for g in clusters(con):
        if round_id in g:
            return g
    return [round_id]


def confirm(con: duckdb.DuckDBPyConnection, a: str, b: str, rule: str = "bestaetigt") -> None:
    """Zwei Runden als dieselbe Runde festlegen (auch ohne erfüllte Regeln = Zusammenführen)."""
    if a == b:
        raise ValueError("Eine Runde kann nicht mit sich selbst verknüpft werden")
    facts = load_facts(con)
    for ref in (a, b):
        if ref not in facts:
            raise ValueError(f"Runde {ref!r} nicht gefunden oder noch laufend")
    ga, gb = _cluster_of(con, a), _cluster_of(con, b)
    if ga != gb:
        sources_a = {facts[m].source for m in ga if m in facts}
        sources_b = {facts[m].source for m in gb if m in facts}
        if sources_a & sources_b:
            raise ValueError(
                "Zusammenführen würde zwei Runden derselben Quelle in eine Runde legen; "
                "erst die bestehende Verknüpfung trennen"
            )
    _upsert_user(con, a, b, "confirmed", rule)


def reject(con: duckdb.DuckDBPyConnection, a: str, b: str) -> None:
    """Zwei Runden als verschieden festlegen (auch nach einem Zusammenführen = Trennen)."""
    _upsert_user(con, a, b, "rejected", "abgelehnt")


def accept_proposed(con: duckdb.DuckDBPyConnection) -> tuple[int, list[str]]:
    """Alle Vorschläge bestätigen; Konflikte werden übersprungen und gemeldet."""
    rows = con.execute(
        "select round_a, round_b from round_match where status = 'proposed' "
        "order by minutes_apart, round_a"
    ).fetchall()
    done, skipped = 0, []
    for a, b in rows:
        try:
            confirm(con, a, b)
            done += 1
        except ValueError as exc:
            skipped.append(f"{a} / {b}: {exc}")
    return done, skipped


# --- Anzeige ---


@dataclass(frozen=True)
class MatchRow:
    a: RoundFacts
    b: RoundFacts
    status: str
    confidence: str
    rule: str
    minutes_apart: float | None
    score_delta: int | None
    hole_diff: int | None
    decided_by: str


def list_matches(
    con: duckdb.DuckDBPyConnection, statuses: tuple[str, ...] | None = None
) -> list[MatchRow]:
    """Treffer mit den Fakten beider Runden, älteste Runde zuerst."""
    facts = load_facts(con)
    rows = con.execute(
        "select round_a, round_b, status, confidence, rule, minutes_apart, score_delta, "
        "hole_diff, decided_by from round_match order by round_a"
    ).fetchall()
    out = [
        MatchRow(facts[r[0]], facts[r[1]], *r[2:])
        for r in rows
        if r[0] in facts and r[1] in facts and (statuses is None or r[2] in statuses)
    ]
    return sorted(out, key=lambda m: (m.a.start, m.a.round_id))


def cluster_facts(con: duckdb.DuckDBPyConnection) -> list[list[RoundFacts]]:
    facts = load_facts(con)
    groups = [[facts[m] for m in g if m in facts] for g in clusters(con)]
    return sorted((g for g in groups if len(g) > 1), key=lambda g: min(f.start for f in g))


def leaders_of(members: list[RoundFacts], cfg: DedupConfig) -> dict[str, str]:
    """Führende Quelle je Feldgruppe für eine Gruppe verknüpfter Runden."""
    return {g: leader(members, g, cfg).source for g in GROUPS}


def links_of(con: duckdb.DuckDBPyConnection, round_id: str) -> list[MatchRow]:
    return [m for m in list_matches(con) if round_id in (m.a.round_id, m.b.round_id)]
