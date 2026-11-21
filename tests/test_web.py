"""Weboberfläche: Seiten rendern, Eingaben speichern, Datenehrlichkeit (NULL ist nie 0)."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shotlake.analytics.trend import TrendPoint
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.charts import PointInfo, bars_svg, trend_svg
from tests.test_course_shots import SHOTS, seed
from tests.test_score import seed_round

BASELINE = Path(__file__).parent.parent / "baselines" / "hcp18w-approx-v1.toml"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    data = tmp_path / "d"
    con = open_db(data / "shotlake.duckdb")
    for i, strokes in enumerate([5, 6, 5, 7, 6, 5]):
        key = f"r{i}"
        seed_round(con, key=key, strokes=strokes)
        con.execute(
            "update round set start_utc = ? where round_id = ?",
            [datetime(2026, 7, i + 1), f"t|{key}"],
        )
    con.close()
    return TestClient(create_app(data, tmp_path / "club_map.toml", BASELINE))


def test_all_pages_render(client: TestClient):
    for path in ("/", "/rounds", "/rounds?relevant=1", "/clubs", "/sources", "/?u=yd&w=3"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "<title>" in r.text and "Shotlake" in r.text


def test_dashboard_shows_trend_and_honesty_notes(client: TestClient):
    html = client.get("/").text
    assert "<svg" in html and "gleitender Durchschnitt" in html
    assert "Kein Handicap-Index" in html
    assert "n = 6 von 6" in html
    assert "dedupliziert" in html  # Standard: alle Quellen, dieselbe Runde zählt einmal


def test_empty_database_renders(tmp_path: Path):
    app = create_app(tmp_path / "leer", tmp_path / "c.toml", BASELINE)
    c = TestClient(app)
    for path in ("/", "/rounds", "/clubs", "/sources"):
        assert c.get(path).status_code == 200


def test_round_detail_and_unknown_round(client: TestClient):
    page = client.get("/rounds/t%7Cr2")
    assert page.status_code == 200
    assert "Testplatz" in page.text and "Score Differential" in page.text
    assert "unbereinigtes Brutto" in page.text  # kein Index gesetzt
    assert client.get("/rounds/t%7Cgibtsnicht").status_code == 404
    assert client.get("/rounds/r2").status_code == 200  # auch über den Schlüssel auffindbar


def test_inputs_are_saved_and_shown(client: TestClient):
    r = client.post(
        "/rounds/t%7Cr2",
        data={"relevant": "yes", "hi": "20", "pcc": "1,0", "ags": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/rounds/t%7Cr2#eingaben" in r.headers["location"]
    html = client.get("/rounds/t%7Cr2").text
    assert 'value="20.0"' in html and 'value="1.0"' in html
    assert '<option value="yes" selected>' in html
    assert "berechnet" in html  # mit Index wird der AGS berechnet
    # Leere Felder löschen den Wert; "nein" ist der Standard
    client.post("/rounds/t%7Cr2", data={"relevant": "no"}, follow_redirects=False)
    html = client.get("/rounds/t%7Cr2").text
    assert 'value="20.0"' not in html and '<option value="no" selected>' in html


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"relevant": "vielleicht"}, "ungültig"),
        ({"relevant": "auto", "pcc": "9"}, "PCC"),
        ({"relevant": "auto", "hi": "abc"}, "keine Zahl"),
        ({"relevant": "auto", "ags": "88.5"}, "ganze Zahl"),
    ],
)
def test_invalid_inputs_are_rejected_and_nothing_is_saved(client: TestClient, form, message):
    r = client.post("/rounds/t%7Cr2", data=form)
    assert r.status_code == 400 and message in r.text
    assert "unbereinigtes Brutto" in client.get("/rounds/t%7Cr2").text  # unverändert


def test_source_filter_and_unit_toggle(client: TestClient, tmp_path: Path):
    assert "Keine Runden" in client.get("/rounds?source=arccos").text
    assert client.get("/rounds?source=t").text.count("/rounds/t%7Cr") >= 6
    con = open_db(tmp_path / "d" / "shotlake.duckdb")
    seed(con, SHOTS)
    con.close()
    meter = client.get("/clubs?source=t&u=m").text
    yards = client.get("/clubs?source=t&u=yd").text
    assert "190" in meter and "208" in yards  # Median 190 m bzw. 190/0.9144 = 207,8 yd
    assert "kein Carry" in meter and "zu wenige Schläge" in meter


def test_charts_handle_edge_cases():
    assert trend_svg([], 5) == "" and bars_svg([]) == ""
    one = TrendPoint("r", datetime(2026, 7, 1), 20.0, None, 5, True)
    svg = trend_svg([one], 5)
    assert svg.count('class="dot') == 1 and "nan" not in svg.lower()
    svg = bars_svg([("Abschlag", -1.5), ("Putten", 0.0)], "/Loch")
    assert "-1.50/Loch" in svg and "rect" in svg


def test_default_is_not_relevant_and_toggle_in_list(client: TestClient):
    html = client.get("/rounds").text
    assert html.count('aria-label="Handicap-relevant: nein') == 6  # Standard: nein
    assert 'aria-label="Handicap-relevant: ja' not in html
    r = client.post(
        "/rounds/t%7Cr2/relevant",
        data={"relevant": "yes", "back": "/rounds?source=t"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/rounds?source=t"
    html = client.get("/rounds").text
    assert html.count('aria-label="Handicap-relevant: ja') == 1
    assert client.get("/rounds?relevant=1").text.count("/rounds/t%7Cr") == 2  # Link + Formular
    assert (
        client.post("/rounds/t%7Cr2/relevant", data={"relevant": "vielleicht"}).status_code == 400
    )
    assert client.post("/rounds/t%7Cnix/relevant", data={"relevant": "yes"}).status_code == 404


def test_toggle_redirect_only_to_local_paths(client: TestClient):
    for evil in ("https://evil.example/x", "//evil.example/x"):
        r = client.post(
            "/rounds/t%7Cr2/relevant",
            data={"relevant": "yes", "back": evil},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/rounds"


def test_dashboard_filter_uses_only_user_marked_rounds(client: TestClient):
    assert '<circle class="dot rel"' not in client.get("/").text  # nichts als relevant gesetzt
    client.post("/rounds/t%7Cr2/relevant", data={"relevant": "yes"}, follow_redirects=False)
    assert client.get("/").text.count('class="dot rel"') == 1


def test_ags_is_explained_in_plain_language(client: TestClient):
    html = client.get("/rounds/t%7Cr2").text
    assert "Adjusted Gross Score" in html and "Netto-Doppelbogey" in html
    assert "Bereinigtes Brutto (AGS)" in html and "AGS manuell (bereinigtes Brutto)" in html


def test_tee_values_can_be_set_and_change_the_differential(client: TestClient):
    before = client.get("/rounds/t%7Cr2").text
    assert 'name="all_rating"' in before and "Platzwerte (Rating und Slope)" in before
    r = client.post(
        "/rounds/t%7Cr2/tee",
        data={"all_rating": "65,0", "all_slope": "120", "front9_rating": "", "front9_slope": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("#platzwerte")
    after = client.get("/rounds/t%7Cr2").text
    assert 'value="65.0"' in after and 'value="120"' in after
    assert before != after
    # Wert gilt für alle Runden mit diesem Platz/Tee: auch andere Runde zeigt ihn
    assert 'value="65.0"' in client.get("/rounds/t%7Cr3").text
    # beide Felder leer: löschen
    client.post(
        "/rounds/t%7Cr2/tee", data={"all_rating": "", "all_slope": ""}, follow_redirects=False
    )
    assert 'value="65.0"' not in client.get("/rounds/t%7Cr2").text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"all_rating": "abc", "all_slope": "120"}, "Rating als Zahl"),
        ({"all_rating": "71.2", "all_slope": ""}, "ganze Zahl"),
        ({"all_rating": "10", "all_slope": "120"}, "Rating erlaubt"),
        ({"all_rating": "71.2", "all_slope": "300"}, "Slope erlaubt"),
        ({"front9_rating": "70", "front9_slope": "120"}, "Vorderneun: Rating erlaubt"),
    ],
)
def test_tee_values_validation(client: TestClient, form, message):
    r = client.post("/rounds/t%7Cr2/tee", data=form)
    assert r.status_code == 400 and message in r.text
    assert 'value="120"' not in client.get("/rounds/t%7Cr2").text  # nichts gespeichert


def test_trend_points_have_tooltip_and_link():
    one = TrendPoint("t|r1", datetime(2026, 7, 1), 20.0, None, 5, True)
    info = {"t|r1": PointInfo("01.07.2026, Platz\nBrutto 98", "/rounds/t|r1?u=yd")}
    svg = trend_svg([one], 5, info=info)
    assert '<a class="pt-link" href="/rounds/t|r1?u=yd">' in svg
    assert "01.07.2026, Platz\nBrutto 98" in svg
    assert "pt-link" not in trend_svg([one], 5)  # ohne Angaben: nur Tooltip, kein Link


def test_dashboard_trend_links_to_round(client: TestClient):
    html = client.get("/?source=t").text
    assert 'class="pt-link" href="/rounds/t|r' in html and "Klick: Runde öffnen" in html


def test_bars_with_intervals_draw_whiskers_and_tooltip():
    svg = bars_svg([("Putten", -0.2), ("Abschlag", 0.1)], "/Loch", {"Putten": (-0.35, -0.05, 12)})
    assert svg.count('class="ci-bar"') == 1
    assert "Putten: -0.20/Loch; 95-%-Intervall -0.35 bis -0.05/Loch; n = 12 Runden" in svg
    assert 'class="ci-bar"' not in bars_svg([("Putten", -0.2)], "/Loch")
