"""Eigene Platz-Stammdaten: Pflege, Zuordnung zu Plätzen der Quellen, Rating als Rückfall."""

from datetime import datetime
from pathlib import Path

import pytest

from shotlake import courses
from shotlake.analytics.score import round_scores
from shotlake.courses import CourseError, MasterIndex, tee_key
from shotlake.db import open_db


@pytest.fixture
def con(tmp_path: Path):
    c = open_db(tmp_path / "t.duckdb")
    yield c
    c.close()


def add_course(
    con, course_id="arccos|1", source="arccos", name="Golfclub Herrnhof", lat=None, lon=None
):
    con.execute(
        "insert or ignore into course (course_id, source, name, lat, lon, par_total) "
        "values (?,?,?,?,?,72)",
        [course_id, source, name, lat, lon],
    )


def add_round(con, rid, course_id="arccos|1", tee=None, rating=None, slope=None, holes=18, day=1):
    """18 (oder 9) Löcher Par 4, Bogey-Golf: Score 90 bzw. 45."""
    source = course_id.split("|")[0]
    con.execute(
        "insert into round (round_id, source, round_key, course_id, start_utc, holes_played, "
        "tee_name, tee_rating, tee_slope, in_progress, exclude_from_stats, simulated, "
        "content_hash, raw_hash, import_run_id) "
        "values (?,?,?,?,?,?,?,?,?,false,false,false,'h','r','i')",
        [
            rid,
            source,
            rid.split("|")[-1],
            course_id,
            datetime(2026, 6, day),
            holes,
            tee,
            rating,
            slope,
        ],
    )
    for n in range(1, holes + 1):
        con.execute(
            "insert into hole values (?,?,?,?,?,?,?,?,?,?)",
            [rid, n, 4, n, 5, 2, 0, None, None, None],
        )


def score(con, rid):
    (s,) = [r for r in round_scores(con) if r.round_id == rid]
    return s


# --- Pflege -------------------------------------------------------------------------------


def test_create_edit_and_delete_master_with_unique_slug(con):
    a = courses.save_master(
        con, name="Golfclub Herrnhof", city=" Neumarkt ", aliases=["GC Herrnhof", " "]
    )
    b = courses.save_master(con, name="Golfclub Herrnhof")
    assert (a, b) == ("golfclub-herrnhof", "golfclub-herrnhof-2")
    m = courses.get_master(con, a)
    assert m and m.city == "Neumarkt" and m.aliases == ("GC Herrnhof",)
    courses.save_master(con, master_id=a, name="GC Herrnhof", par_total=72, default_tee="Orange")
    assert courses.get_master(con, a).par_total == 72  # type: ignore[union-attr]
    courses.set_tee(con, a, "Orange", rating=72.1, slope=124)
    courses.delete_master(con, a)
    assert courses.get_master(con, a) is None
    assert con.execute("select count(*) from course_master_tee").fetchone() == (0,)


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"rating": 20.0, "slope": 120}, "Rating"),
        ({"rating": 72.0, "slope": 20}, "Slope"),
        ({"rating": 72.0, "slope": 120, "part": "halb"}, "Teil"),
        ({"rating": 72.0, "slope": 120, "par": 5}, "Par"),
    ],
)
def test_implausible_tee_values_are_rejected(con, kwargs, text):
    mid = courses.save_master(con, name="X")
    with pytest.raises(CourseError, match=text):
        courses.set_tee(con, mid, "Gelb", **kwargs)
    with pytest.raises(CourseError, match="Tee-Name"):
        courses.set_tee(con, mid, " ", rating=72.0, slope=120)
    assert con.execute("select count(*) from course_master_tee").fetchone() == (0,)


def test_master_validation(con):
    with pytest.raises(CourseError, match="Platzname"):
        courses.save_master(con, name=" ")
    with pytest.raises(CourseError, match="zusammen"):
        courses.save_master(con, name="X", lat=49.0)
    with pytest.raises(CourseError, match="nicht gefunden"):
        courses.save_master(con, master_id="gibtsnicht", name="X")
    with pytest.raises(CourseError, match="nicht gefunden"):
        courses.set_tee(con, "gibtsnicht", "Gelb", rating=72.0, slope=120)


def test_same_tee_in_other_spelling_updates_instead_of_duplicating(con):
    mid = courses.save_master(con, name="X")
    courses.set_tee(con, mid, "Blau", rating=70.5, slope=130)
    courses.set_tee(con, mid, "Blue", rating=70.9, slope=131)  # derselbe Tee
    (tee,) = courses.get_master(con, mid).tees  # type: ignore[union-attr]
    assert (tee.name, tee.rating, tee.slope) == ("Blau", 70.9, 131)


def test_deleting_the_default_tee_clears_it(con):
    mid = courses.save_master(con, name="X", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    assert courses.delete_tee(con, mid, "Orange") == 1
    assert courses.get_master(con, mid).default_tee is None  # type: ignore[union-attr]


def test_tee_keys_unify_colours_languages_and_hex():
    assert tee_key("Blue") == tee_key("Blau") == tee_key("#0000FF") == tee_key(" blau ")
    assert tee_key("Weiß") == tee_key("White") and tee_key("Gelb") != tee_key("Orange")
    assert tee_key("Championship") == "championship" and tee_key(None) == ""


# --- Zuordnung ----------------------------------------------------------------------------


def test_matching_by_link_name_alias_geo_and_explicit_none(con):
    mid = courses.save_master(
        con, name="Golfclub Herrnhof", aliases=["GC Herrnhof 9"], lat=49.28, lon=11.46
    )
    add_course(con, "a|name", name="golfclub  HERRNHOF")
    add_course(con, "b|alias", name="GC Herrnhof 9")
    add_course(con, "c|geo", name="Platz am See", lat=49.2805, lon=11.4605)
    add_course(con, "d|far", name="Platz am See", lat=49.5, lon=11.9)
    add_course(con, "e|link", name="Irgendein Name")
    add_course(con, "f|none", name="Golfclub Herrnhof")
    courses.link_course(con, "e|link", mid)
    courses.link_course(con, "f|none", None)
    index = MasterIndex.load(con)
    assert index is not None
    how = {
        c: (index.master_for(c) or (None, None))[1]
        for c in ["a|name", "b|alias", "c|geo", "d|far", "e|link", "f|none"]
    }
    assert how == {"a|name": "Name", "b|alias": "Name", "c|geo": "Koordinaten", "d|far": None,
                   "e|link": "Zuordnung", "f|none": None}  # fmt: skip
    courses.unlink_course(con, "f|none")
    index = MasterIndex.load(con)
    assert index is not None and index.master_for("f|none") is not None  # wieder automatisch


def test_link_validation_and_no_index_without_masters(con):
    add_course(con)
    assert MasterIndex.load(con) is None
    with pytest.raises(CourseError, match="Platz"):
        courses.link_course(con, "gibts|nicht", None)
    with pytest.raises(CourseError, match="Stammplatz"):
        courses.link_course(con, "arccos|1", "gibtsnicht")


def test_tee_choice_by_name_default_single_and_unknown(con):
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    courses.set_tee(con, mid, "Blau", rating=70.5, slope=130)
    add_course(con)
    index = MasterIndex.load(con)
    assert index is not None
    assert index.rating_for("arccos|1", "Blue").tee == "Blau"  # type: ignore[union-attr]
    assert index.rating_for("arccos|1", None).tee == "Orange"  # type: ignore[union-attr]
    assert index.rating_for("arccos|1", "").tee == "Orange"  # type: ignore[union-attr]
    assert index.rating_for("arccos|1", "Championship") is None  # kein Raten bei fremdem Tee
    courses.save_master(con, master_id=mid, name="Golfclub Herrnhof")  # ohne Standard-Tee
    index = MasterIndex.load(con)
    assert index is not None and index.rating_for("arccos|1", None) is None  # zwei Tees: unklar
    courses.delete_tee(con, mid, "Blau")
    index = MasterIndex.load(con)
    assert index is not None and index.rating_for("arccos|1", None).tee == "Orange"  # type: ignore[union-attr]


def test_import_from_source_course_copies_tees_and_links(con):
    add_course(con, "garmin_platform|1", "garmin_platform", "Golfclub Herrnhof", 49.28, 11.46)
    con.execute("insert into course_tee values ('garmin_platform|1','Orange','WOMEN',72.1,124)")
    con.execute("insert into course_tee values ('garmin_platform|1','Rot','WOMEN',74.3,129)")
    con.execute("insert into course_tee values ('garmin_platform|1','Leer','WOMEN',null,null)")
    con.execute("insert into course_tee values ('garmin_platform|1','Kaputt','WOMEN',5.0,999)")
    mid = courses.import_from_course(con, "garmin_platform|1")
    m = courses.get_master(con, mid)
    assert m and (m.lat, m.par_total) == (49.28, 72)
    assert {(t.name, t.rating, t.slope) for t in m.tees} == {
        ("Orange", 72.1, 124),
        ("Rot", 74.3, 129),
    }
    assert con.execute("select master_id from course_master_link").fetchall() == [(mid,)]
    with pytest.raises(CourseError):
        courses.import_from_course(con, "gibts|nicht")


# --- Rating als Rückfall -------------------------------------------------------------------


def test_round_without_rating_gets_differential_from_master(con):
    add_course(con)
    add_round(con, "arccos|r1")
    assert score(con, "arccos|r1").differential is None  # vorher: Rating/Slope fehlen
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    s = score(con, "arccos|r1")
    assert s.gross == 90 and s.differential == pytest.approx((90 - 72.1) * 113 / 124, abs=0.05)
    assert s.rating_source == "Stammdaten Golfclub Herrnhof / Orange"


def test_source_rating_wins_over_master(con):
    add_course(con)
    add_round(con, "arccos|r1", tee="Orange", rating=70.0, slope=120)
    mid = courses.save_master(con, name="Golfclub Herrnhof")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    s = score(con, "arccos|r1")
    assert s.differential == pytest.approx((90 - 70.0) * 113 / 120, abs=0.05)
    assert s.rating_source is None


def test_manual_per_round_values_win_over_master(con):
    add_course(con)
    add_round(con, "arccos|r1")
    con.execute("insert into tee_rating values ('arccos|1','','all',71.0,122)")  # tee-set
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    assert score(con, "arccos|r1").differential == pytest.approx((90 - 71.0) * 113 / 122, abs=0.05)


def test_unknown_tee_stays_without_rating_and_nine_hole_parts_work(con):
    add_course(con)
    add_round(con, "arccos|r1", tee="Championship")
    add_round(con, "arccos|r2", holes=9, day=2)
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    courses.set_tee(con, mid, "Orange", rating=36.0, slope=122, part="front9")
    assert score(con, "arccos|r1").differential is None
    nine = score(con, "arccos|r2")
    assert (
        nine.differential is not None
        and nine.rating_source == "Stammdaten Golfclub Herrnhof / Orange"
    )


def test_master_for_other_course_does_not_leak(con):
    add_course(con)
    add_course(con, "arccos|2", name="Ganz anderer Platz")
    add_round(con, "arccos|r1", course_id="arccos|2")
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    assert score(con, "arccos|r1").differential is None


def test_source_courses_overview_reports_matches_and_unmatched_tees(con):
    add_course(con)
    add_course(con, "bebrassie|9", "bebrassie", "Neumarkt")
    add_round(con, "arccos|r1")
    add_round(con, "arccos|r2", tee="Championship", day=2)
    add_round(con, "bebrassie|r3", course_id="bebrassie|9", tee="Blue", rating=70.5, slope=130)
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    rows = {c.course_id: c for c in courses.source_courses(con)}
    herrnhof = rows["arccos|1"]
    assert (herrnhof.rounds, herrnhof.rounds_no_rating, herrnhof.how) == (2, 1, "Name")
    assert herrnhof.master and herrnhof.unmatched_tees == ("Championship",)  # r1 hat Standard-Tee
    assert rows["bebrassie|9"].master is None and rows["bebrassie|9"].rounds_no_rating == 0
    assert herrnhof.tees == {"": 1, "Championship": 1}


def test_export_lists_the_new_tables(tmp_path):
    from shotlake.export import TABLES

    assert {"course_master", "course_master_tee", "course_master_link"} <= set(TABLES)
    _ = tmp_path


# --- Weboberfläche und Kommandozeile -------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from shotlake.web.app import create_app

    data = tmp_path / "d"
    c = open_db(data / "shotlake.duckdb")
    add_course(c)
    add_round(c, "arccos|r1")
    c.close()
    base = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"
    return TestClient(create_app(data, tmp_path / "club_map.toml", base), follow_redirects=False)


def test_web_course_workflow_from_creation_to_differential(client):
    page = client.get("/courses").text
    assert "Golfclub Herrnhof" in page and "keiner" in page and "Als Stammplatz übernehmen" in page
    r = client.post("/courses", data={"name": "Golfclub Herrnhof"})
    assert r.status_code == 303 and r.headers["location"].startswith("/courses/golfclub-herrnhof")
    r = client.post(
        "/courses/golfclub-herrnhof/tee",
        data={"tee_name": "Orange", "part": "all", "rating": "72,1", "slope": "124", "par": "72"},
    )
    assert r.status_code == 303 and "gespeichert" in r.headers["location"].replace("%20", " ")
    form = {"name": "Golfclub Herrnhof", "default_tee": "Orange", "aliases": "GC Herrnhof, ",
            "city": "Neumarkt", "lat": "49,28", "lon": "11,46"}  # fmt: skip
    client.post("/courses/golfclub-herrnhof", data=form)
    edit = client.get("/courses/golfclub-herrnhof").text
    assert "Orange" in edit and "72.1" in edit and "Standard" in edit and "GC Herrnhof" in edit
    assert 'value="49.28"' in edit
    overview = client.get("/courses").text
    assert "(Name)" in overview and "Orange" in overview
    rounds = client.get("/rounds").text
    assert "Stammdaten Golfclub Herrnhof / Orange" in rounds  # Differential statt „Rating fehlt"
    assert "Rating/Slope fehlen" not in rounds


def test_web_validation_messages_and_unknown_master(client):
    client.post("/courses", data={"name": "X"})
    for data, text in [
        ({"tee_name": "Gelb", "rating": "abc", "slope": "120"}, "keine Zahl"),
        ({"tee_name": "Gelb", "rating": "20", "slope": "120"}, "unplausibel"),
        ({"tee_name": "Gelb", "rating": "", "slope": ""}, "angeben"),
    ]:
        loc = client.post("/courses/x/tee", data=data).headers["location"]
        assert text in loc.replace("%20", " ")
    assert "Platzname" in client.post("/courses", data={"name": " "}).headers["location"]
    assert client.get("/courses/gibtsnicht").headers["location"].startswith("/courses")
    assert "nicht" in client.post("/courses/x/delete", data={}).headers["location"]
    assert client.get("/courses/x").status_code == 200  # ohne Bestätigung nichts gelöscht


def test_web_import_link_and_delete(client, tmp_path):
    r = client.post("/courses/import", data={"course_id": "arccos|1"})
    assert r.headers["location"].startswith("/courses/golfclub-herrnhof")
    assert "Zuordnung" in client.get("/courses").text
    client.post("/courses/link", data={"course_id": "arccos|1", "master": "none"})
    assert "keiner" in client.get("/courses").text
    client.post("/courses/link", data={"course_id": "arccos|1", "master": "auto"})
    assert "(Name)" in client.get("/courses").text
    client.post("/courses/golfclub-herrnhof/delete", data={"confirm": "yes"})
    assert "Noch kein Stammplatz" in client.get("/courses").text
    assert client.get("/rounds").status_code == 200


def test_cli_course_commands(tmp_path, capsys):
    from shotlake.cli import main

    data = tmp_path / "d"
    c = open_db(data / "shotlake.duckdb")
    add_course(c)
    add_round(c, "arccos|r1")
    c.close()

    def run(*args):
        code = main(["--data-dir", str(data), *args])
        return code, capsys.readouterr()

    assert (
        run("course-add", "Golfclub Herrnhof", "--default-tee", "Orange", "--alias", "GC H")[0] == 0
    )
    assert (
        run("course-tee", "golfclub-herrnhof", "Orange", "--rating", "72.1", "--slope", "124")[0]
        == 0
    )
    code, out = run("courses")
    assert code == 0 and "golfclub-herrnhof: Golfclub Herrnhof, Standard-Tee Orange" in out.out
    assert "arccos|1: Golfclub Herrnhof, 1 Runden -> golfclub-herrnhof (Name)" in out.out
    code, out = run("course-tee", "golfclub-herrnhof", "Orange", "--rating", "20", "--slope", "124")
    assert code == 2 and "Fehler:" in out.err and "unplausibel" in out.err
    assert run("course-link", "arccos|1", "keiner")[0] == 0
    assert "ohne Stammplatz" in run("courses")[1].out
    code, out = run("course-import", "arccos|1")  # Quelle ohne Tees: Stammplatz ohne Tees
    assert code == 0 and "golfclub-herrnhof-2" in out.out and "Tees: keine" in out.out
    assert "Stammdaten" not in run("rounds")[1].out
    run("course-tee", "golfclub-herrnhof-2", "Orange", "--rating", "72.1", "--slope", "124")
    run("course-add", "Zweiter", "--default-tee", "Orange")
    code, out = run("rounds")
    assert code == 0 and "Rating von Stammdaten Golfclub Herrnhof / Orange" in out.out


def test_label_only_when_master_contributed_values(con):
    add_course(con)
    add_round(con, "arccos|r1")
    con.execute("insert into tee_rating values ('arccos|1','','all',71.0,122)")  # nur manuell
    mid = courses.save_master(con, name="Golfclub Herrnhof", default_tee="Orange")
    courses.set_tee(con, mid, "Orange", rating=72.1, slope=124)
    s = score(con, "arccos|r1")
    assert s.differential == pytest.approx((90 - 71.0) * 113 / 122, abs=0.05)
    assert s.rating_source is None  # die Werte kommen von dir, nicht aus den Stammdaten
