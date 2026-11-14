"""Dedup: Abgleich, Entscheidungen des Nutzers, Cluster und führende Quelle."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shotlake import dedup
from shotlake.db import open_db
from shotlake.dedup import DedupConfig, load_config

T0 = datetime(2026, 7, 1, 10, 0)
CONFIG_FILE = Path(__file__).parent.parent / "config" / "dedup.toml"


@pytest.fixture
def con(tmp_path: Path):
    c = open_db(tmp_path / "t.duckdb")
    yield c
    c.close()


def add_round(
    con, source: str, key: str, *, minutes: float = 0, holes: int = 9, gross: int | None = 50,
    course: str = "Golfclub Neumarkt", lat: float | None = None, lon: float | None = None,
    shots: bool = False, rating: tuple[float, int] | None = None, day: datetime = T0,
):  # fmt: skip
    cid = f"{source}|{course}"
    con.execute(
        "insert or ignore into course values (?,?,?,null,null,?,?,72)",
        [cid, source, course, lat, lon],
    )
    rid = f"{source}|{key}"
    con.execute(
        "insert into round (round_id, source, round_key, course_id, start_utc, holes_played, "
        "gross_strokes, tee_rating, tee_slope, in_progress, exclude_from_stats, simulated, "
        "content_hash, raw_hash, import_run_id) values (?,?,?,?,?,?,?,?,?, false, false, false, "
        "'h', 'r', 'i')",
        [rid, source, key, cid, day + timedelta(minutes=minutes), holes, gross,
         rating[0] if rating else None, rating[1] if rating else None],
    )  # fmt: skip
    if shots:
        con.execute(
            "insert into course_shot (shot_id, round_id, hole_number, shot_order, "
            "exclude_from_stats, content_hash, raw_hash, import_run_id) "
            "values (?, ?, 1, 1, false, 'h', 'r', 'i')",
            [f"{rid}|1|1", rid],
        )
    return rid


def statuses(con) -> dict[tuple[str, str], str]:
    rows = con.execute("select round_a, round_b, status from round_match").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


CFG = DedupConfig()


# --- Konfiguration ---


def test_repo_config_has_club_aliases_priorities_and_valid_values():
    cfg = load_config(CONFIG_FILE)
    # alter und neuer Name sowie der 9-Loch-Platz gehören zum selben Club
    keys = {cfg.clubs[dedup._norm(n)] for n in ("Golfclub Herrnhof", "Golfclub Neumarkt",
                                                  "Neumarkt Front-9")}  # fmt: skip
    assert len(keys) == 1
    assert cfg.priority["shots"][0] == "arccos" and cfg.priority["score"][0] == "bebrassie"
    assert cfg.max_minutes == 90 and cfg.auto_score_tolerance == 0


def test_config_validation_errors(tmp_path: Path):
    bad = tmp_path / "d.toml"
    bad.write_text('[priority]\nfarben = ["a"]\n')
    with pytest.raises(ValueError, match="unbekannte Feldgruppe"):
        load_config(bad)
    bad.write_text('[[club]]\nname = "A"\naliases = ["X"]\n[[club]]\nname = "B"\naliases = ["X"]\n')
    with pytest.raises(ValueError, match="mehreren Clubs"):
        load_config(bad)
    assert load_config(tmp_path / "gibtsnicht.toml") == DedupConfig()


# --- Abgleich ---


def test_exact_unique_match_is_linked_automatically(con):
    a = add_round(con, "garmin_platform", "g1", minutes=0, gross=50)
    b = add_round(con, "arccos", "a1", minutes=20, gross=50)
    report = dedup.run(con, CFG)
    assert (report.auto, report.proposed) == (1, 0)
    row = con.execute("select status, confidence, rule from round_match").fetchone()
    assert row == ("auto", "hoch", "gleicher_club_zeit_loecher_score")
    assert dedup.clusters(con) == [sorted([a, b])]


def test_different_score_is_only_proposed_and_far_apart_scores_never_match(con):
    add_round(con, "garmin_platform", "g1", gross=50)
    add_round(con, "arccos", "a1", gross=54)  # 4 Schläge Unterschied: Vorschlag
    add_round(con, "bebrassie", "b1", gross=75)  # 25 Schläge: nicht dieselbe Runde
    dedup.run(con, CFG)
    rows = con.execute("select status, confidence, rule from round_match").fetchall()
    assert rows == [("proposed", "mittel", "score_weicht_ab")]
    assert dedup.clusters(con) == []  # Vorschläge verknüpfen noch nichts


def test_hole_count_and_time_and_club_rules(con):
    add_round(con, "garmin_platform", "g1", holes=9, gross=50)
    add_round(con, "arccos", "far", minutes=200, holes=9, gross=50)  # zu weit auseinander
    add_round(con, "bebrassie", "other", holes=9, gross=50, course="Ganz Anderer Club")
    dedup.run(con, CFG)
    assert statuses(con) == {}
    # 8 gegen 9 Löcher: nur Vorschlag mit niedriger Konfidenz
    add_round(con, "arccos", "eight", minutes=10, holes=8, gross=44)
    dedup.run(con, CFG)
    row = con.execute("select status, confidence, rule from round_match").fetchone()
    assert row == ("proposed", "niedrig", "lochzahl_weicht_ab")
    # 3 Löcher Unterschied: nichts
    con.execute("delete from round_match")
    strict = DedupConfig(max_hole_difference=0)
    dedup.run(con, strict)
    assert statuses(con) == {}


def test_ambiguity_gives_proposals_for_all_candidates(con):
    g = add_round(con, "garmin_platform", "g1", gross=50)
    a1 = add_round(con, "arccos", "a1", minutes=5, gross=50)
    a2 = add_round(con, "arccos", "a2", minutes=45, gross=50)
    dedup.run(con, CFG)
    st = statuses(con)
    assert set(st.values()) == {"proposed"} and len(st) == 2
    rules = {r[0] for r in con.execute("select rule from round_match").fetchall()}
    assert rules == {"mehrdeutig"}
    # Nutzer entscheidet: g gehört zu a2
    dedup.confirm(con, g, a2)
    dedup.run(con, CFG)
    assert statuses(con)[dedup.pair_key(g, a2)] == "confirmed"
    assert dedup.pair_key(g, a1) not in statuses(
        con
    )  # a1 ist neben bestätigtem Partner kein Kandidat


def test_same_source_never_matches_and_unfinished_rounds_are_ignored(con):
    add_round(con, "arccos", "a1", gross=50)
    add_round(con, "arccos", "a2", minutes=10, gross=50)
    open_round = add_round(con, "garmin_platform", "g1", gross=50)
    con.execute("update round set in_progress = true where round_id = ?", [open_round])
    dedup.run(con, CFG)
    assert statuses(con) == {}


def test_club_by_alias_and_by_coordinates(con):
    # alter Name (Alias aus der Config) gegen neuen Namen
    cfg = load_config(CONFIG_FILE)
    add_round(con, "arccos", "a1", course="Golfclub Herrnhof", gross=50)
    add_round(con, "bebrassie", "b1", course="Golfclub Neumarkt", gross=50)
    dedup.run(con, cfg)
    assert set(statuses(con).values()) == {"auto"}
    assert statuses(con)  # ohne Config (kein Alias) wären die Namen verschieden:
    con.execute("delete from round_match")
    dedup.run(con, DedupConfig())
    assert statuses(con) == {}
    # Koordinaten innerhalb von 2 km: derselbe Club trotz verschiedener Namen
    con.execute("delete from round")
    add_round(con, "garmin_platform", "g1", course="Alter Name", lat=49.30, lon=11.39, gross=50)
    add_round(con, "bebrassie", "b1", course="Neuer Name", lat=49.305, lon=11.395, gross=50)
    dedup.run(con, DedupConfig())
    assert set(statuses(con).values()) == {"auto"}


def test_three_sources_form_one_cluster(con):
    g = add_round(con, "garmin_platform", "g1", gross=50)
    a = add_round(con, "arccos", "a1", minutes=3, gross=50)
    b = add_round(con, "bebrassie", "b1", minutes=6, gross=50)
    dedup.run(con, CFG)
    assert dedup.clusters(con) == [sorted([g, a, b])]


# --- Entscheidungen des Nutzers ---


def test_user_decisions_survive_reruns_and_override_rules(con):
    g = add_round(con, "garmin_platform", "g1", gross=50)
    a = add_round(con, "arccos", "a1", gross=50)
    dedup.run(con, CFG)
    assert statuses(con)[dedup.pair_key(g, a)] == "auto"
    dedup.reject(con, g, a)  # Nutzer trennt
    for _ in range(2):
        report = dedup.run(con, CFG)
    assert statuses(con)[dedup.pair_key(g, a)] == "rejected"  # wird nie wieder automatisch gebildet
    assert dedup.clusters(con) == [] and report.kept_user_decisions == 1
    # Zusammenführen entgegen den Regeln
    far = add_round(con, "bebrassie", "b1", minutes=300, gross=99)
    dedup.confirm(con, g, far)
    dedup.run(con, CFG)
    assert dedup.clusters(con) == [sorted([g, far])]
    row = con.execute(
        "select status, decided_by, rule from round_match where round_a = ? or round_b = ?",
        [far, far],
    ).fetchone()
    assert row == ("confirmed", "user", "bestaetigt")


def test_merge_that_puts_two_rounds_of_one_source_together_is_refused(con):
    g = add_round(con, "garmin_platform", "g1", gross=50)
    a1 = add_round(con, "arccos", "a1", gross=50)
    a2 = add_round(con, "arccos", "a2", minutes=300, gross=50)
    dedup.run(con, CFG)
    with pytest.raises(ValueError, match="derselben Quelle"):
        dedup.confirm(con, g, a2)  # g ist schon mit a1 verknüpft
    dedup.reject(con, g, a1)
    dedup.confirm(con, g, a2)  # nach dem Trennen erlaubt
    assert dedup.clusters(con) == [sorted([g, a2])]
    with pytest.raises(ValueError, match="mit sich selbst"):
        dedup.confirm(con, g, g)
    with pytest.raises(ValueError, match="nicht gefunden"):
        dedup.confirm(con, g, "arccos|nix")


def test_accept_all_proposed_and_resolve_references(con):
    add_round(con, "garmin_platform", "g1", gross=50)
    add_round(con, "arccos", "a1", gross=54)
    add_round(con, "garmin_platform", "g2", minutes=600, gross=60)
    add_round(con, "arccos", "a2", minutes=605, gross=63)
    dedup.run(con, CFG)
    assert {s for s in statuses(con).values()} == {"proposed"}
    done, skipped = dedup.accept_proposed(con)
    assert done == 2 and skipped == [] and len(dedup.clusters(con)) == 2
    assert dedup.resolve(con, "g1") == "garmin_platform|g1"
    with pytest.raises(ValueError, match="Treffer"):
        dedup.resolve(con, "gibtsnicht")


# --- Führende Quelle ---


def test_leader_per_field_group_with_fallback_to_data(con):
    g = add_round(con, "garmin_platform", "g1", gross=50, shots=True, rating=(74.3, 129))
    a = add_round(con, "arccos", "a1", gross=50, shots=True)
    b = add_round(con, "bebrassie", "b1", gross=50, shots=False)
    dedup.run(con, CFG)
    # Score: bebrassie > garmin > arccos; Schläge: arccos > bebrassie > garmin
    assert dedup.excluded_rounds(con, CFG, "score") == {g, a}
    assert dedup.excluded_rounds(con, CFG, "shots") == {g, b}  # bebrassie hat keine Schläge
    # Rating: kein Bebrassie-Wert -> Garmin liefert Rating/Slope für die verknüpften Runden
    donors = dedup.rating_donor(con, CFG)
    assert donors[b] == (74.3, 129, "garmin_platform") and donors[a] == donors[b]
    assert g not in donors
    with pytest.raises(ValueError):
        dedup.excluded_rounds(con, CFG, "farben")


def test_unlinked_rounds_are_never_excluded(con):
    add_round(con, "garmin_platform", "solo", gross=50)
    dedup.run(con, CFG)
    assert dedup.excluded_rounds(con, CFG, "score") == set()
