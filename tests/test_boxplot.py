"""Boxplot der Schlagdistanz: Berechnung (Quartile, Antennen, Ausreißer) und Darstellung."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shotlake.analytics.course_shots import club_distances
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.charts import BoxRow, box_row, boxplot_svg
from tests.test_course_shots import seed

REPO = Path(__file__).parent.parent
CLUB_MAP = REPO / "config" / "club_map.toml"
BASELINE = REPO / "baselines" / "hcp40w-approx-v1.toml"


def test_tukey_whiskers_and_outliers() -> None:
    values = [10, 11, 12, 13, 14, 15, 16, 17, 18, 60.0]  # 60 liegt weit außerhalb
    row = box_row("iron_7", values, q1=12.25, median=14.5, q3=16.75)
    assert row.whisker_lo == 10 and row.whisker_hi == 18  # äußerster Wert innerhalb 1,5 x Kasten
    assert row.outliers == (60.0,) and row.n == 10


def test_equal_values_collapse_to_a_point_without_errors() -> None:
    row = box_row("x", [5.0, 5.0, 5.0], 5.0, 5.0, 5.0)
    assert (row.whisker_lo, row.whisker_hi, row.outliers) == (5.0, 5.0, ())


def test_svg_has_box_median_whiskers_outliers_and_no_median_number() -> None:
    row = BoxRow("driver", 8, 100.0, 136.0, 165.0, 60.0, 220.0, (260.0,), True)
    svg = boxplot_svg([row], factor=1.0, unit="m")
    assert 'class="chart box"' in svg and svg.count('class="box-rect"') == 1
    assert svg.count('class="box-out"') == 1 and svg.count('class="whisker"') == 4
    assert svg.count('class="med"') == 1
    # Der Median steht nur im Tooltip, nicht als Zahl im Diagramm
    assert "<title>driver: Median 136 m;" in svg
    assert "Kasten (Q1 bis Q3) 100 bis 165 m" in svg and "Ausreißer 1; n = 8" in svg
    assert 'class="val"' not in svg and ">136<" not in svg
    assert 'class="hit"' in svg  # Trefferfläche über der ganzen Zeile


def test_svg_converts_to_yards_and_fades_small_samples() -> None:
    row = BoxRow("iron_9", 3, 50.0, 60.0, 70.0, 40.0, 80.0, (), ok=False)
    svg = boxplot_svg([row], factor=1.0936, unit="yd")
    assert 'class="thin"' in svg and "Median 66 yd" in svg
    assert boxplot_svg([]) == ""


def test_club_distances_provide_quartiles_and_values(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    shots = [("c1", "TEE", float(d), "tee", "fairway", False) for d in range(100, 200, 10)]
    seed(con, shots)
    (c,) = club_distances(con, min_n=1)
    assert c.n == 10 and len(c.values) == 10 and c.values == tuple(sorted(c.values))
    assert c.p25_m is not None and c.p75_m is not None and c.median_m is not None
    assert c.p10_m is not None and c.p90_m is not None
    assert c.p10_m < c.p25_m < c.median_m < c.p75_m < c.p90_m <= (c.max_m or 0)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    con = open_db(tmp_path / "d" / "shotlake.duckdb")
    seed(con, [("c1", "TEE", float(d), "tee", "fairway", False) for d in range(100, 200, 10)])
    con.close()
    return TestClient(create_app(tmp_path / "d", CLUB_MAP, BASELINE))


def test_clubs_page_shows_boxplot_and_quartile_columns(client: TestClient) -> None:
    html = client.get("/clubs").text
    assert 'class="chart box"' in html and "Boxplot der Schlagdistanz" in html
    assert "Kasten: mittlere 50 %" in html and "Median (Tooltip)" in html
    header = re.search(r"<thead><tr><th>Schläger</th>(.*?)</tr></thead>", html, re.S)
    assert header is not None
    assert [h for h in re.findall(r"<th[^>]*>(.*?)</th>", header.group(1))][:6] == [
        "n", "P10", "Q1", "Median (m)", "Intervall", "Q3",
    ]  # fmt: skip
    assert "reach-bar" not in html  # alte Darstellung ist ersetzt
    assert "Median (yd)" in client.get("/clubs?u=yd").text


def test_clubs_page_date_filter(client: TestClient) -> None:
    assert 'class="chart box"' in client.get("/clubs?since=2000-01-01").text
    late = client.get("/clubs?since=2999-01-01").text
    assert 'class="chart box"' not in late and "Keine Platzschläge" in late
    assert 'value="2999-01-01"' in late and "Zurücksetzen" in late
    assert 'class="chart box"' not in client.get("/clubs?until=1999-01-01").text
    assert 'class="chart box"' in client.get("/clubs?since=kaputt").text  # ungültig: ignoriert


def test_server_timing_header_and_schema_only_once(client: TestClient) -> None:
    first = client.get("/clubs")
    assert first.headers["Server-Timing"].startswith("app;dur=")
    assert client.get("/clubs?source=t").status_code == 200  # zweiter Aufruf ohne Schema/Migration


def test_clubs_page_shows_median_intervals(client: TestClient) -> None:
    html = client.get("/clubs").text
    assert "95-%-Intervall des Medians" in html and "Median 145 m (95-%-Intervall" in html
    assert re.search(r'<td class="muted">\d+ bis \d+</td>', html)
