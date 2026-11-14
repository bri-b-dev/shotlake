"""Weboberfläche: Seiten rendern, Eingaben speichern, Datenehrlichkeit (NULL ist nie 0)."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shotlake.analytics.trend import TrendPoint
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.charts import bars_svg, trend_svg
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
    assert "Dedup" in html  # Hinweis auf doppelte Runden zwischen Quellen


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
    # Leere Felder löschen den Wert; Vorschlag folgen
    client.post("/rounds/t%7Cr2", data={"relevant": "auto"}, follow_redirects=False)
    html = client.get("/rounds/t%7Cr2").text
    assert 'value="20.0"' not in html and '<option value="auto" selected>' in html


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
    assert svg.count("<circle") == 1 and "nan" not in svg.lower()
    svg = bars_svg([("Abschlag", -1.5), ("Putten", 0.0)], "/Loch")
    assert "-1.50/Loch" in svg and "rect" in svg
