"""Streuungs-Ellipse und Scatter-Diagramm."""

import math
import random
import re
from collections.abc import Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from shotlake.analytics.dispersion import ellipse_95, scatter_groups
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.charts import scatter_svg

REPO = Path(__file__).parent.parent


def sample(n: int = 400, sx: float = 6.0, sy: float = 12.0) -> list[tuple[float, float]]:
    rng = random.Random(1)
    return [(rng.gauss(3.0, sx), rng.gauss(100.0, sy)) for _ in range(n)]


def test_ellipse_recovers_center_and_axes() -> None:
    e = ellipse_95(sample())
    assert e is not None
    assert abs(e.cx - 3.0) < 1.0 and abs(e.cy - 100.0) < 2.0
    assert e.semi_major > e.semi_minor
    assert abs(e.semi_major / e.semi_minor - 2.0) < 0.4  # sigma 12 : 6
    inside = sum(
        1
        for x, y in sample(2000)
        if (
            ((x - e.cx) * math.cos(e.angle_rad) + (y - e.cy) * math.sin(e.angle_rad)) / e.semi_major
        )
        ** 2
        + (((y - e.cy) * math.cos(e.angle_rad) - (x - e.cx) * math.sin(e.angle_rad)) / e.semi_minor)
        ** 2
        <= 1
    )
    assert 0.93 < inside / 2000 < 0.97


def test_too_few_or_flat_points_give_no_ellipse() -> None:
    assert ellipse_95([(0.0, 1.0)] * 5) is None
    assert ellipse_95([(0.0, 100.0 + i) for i in range(10)]) is None  # keine Seitenstreuung


def insert_shots(con, rows: Sequence[tuple[str, str, float, float, float | None]]) -> None:
    from datetime import datetime

    con.execute(
        "insert into session values ('s1','mevo','k','range',?,'h','r')", [datetime(2026, 1, 1)]
    )
    for i, (src, method, side, carry, speed) in enumerate(rows):
        con.execute(
            "insert into shot (shot_id, session_id, source, club_raw, club_type, "
            "measurement_method, carry_m, carry_side_m, ball_speed_ms, flags, content_hash, "
            "raw_hash, import_run_id) values (?,?,?,?,?,?,?,?,?,[],'c','h','r')",
            [f"x{i}", "s1", src, "7i", "iron_7", method, carry, side, speed],
        )


def test_scatter_groups_split_by_source_and_method_and_filter_partial(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    rows = [("mevo", "radar", float(i), 100.0 + i, 35.0) for i in range(10)]
    rows += [("trackman", "sim", float(i), 80.0 + (i * 7) % 5, 35.0) for i in range(9)]
    rows += [("mevo", "radar", 1.0, 20.0, 10.0)]  # Teilschlag
    insert_shots(con, rows)
    groups = {(g.source, g.method): g for g in scatter_groups(con, "iron_7", min_speed_ratio=0.7)}
    assert set(groups) == {("mevo", "radar"), ("trackman", "sim")}
    assert len(groups[("mevo", "radar")].points) == 10  # Teilschlag fehlt
    assert groups[("trackman", "sim")].ellipse is not None
    assert len(scatter_groups(con, "iron_7")[0].points) == 11


def test_scatter_svg_has_points_and_ellipse(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    insert_shots(con, [("mevo", "radar", float(i % 5), 100.0 + i, None) for i in range(12)])
    (group,) = scatter_groups(con, "iron_7")
    svg = scatter_svg(group, 1.0, "m")
    assert svg.count('class="pt"') == 12 and "<polygon" in svg and "<svg" in svg


def test_clubs_page_shows_scatter(tmp_path: Path) -> None:
    con = open_db(tmp_path / "shotlake.duckdb")
    insert_shots(con, [("mevo", "radar", float(i % 5), 100.0 + i, None) for i in range(12)])
    con.close()
    app = create_app(
        tmp_path, REPO / "config" / "club_map.toml", REPO / "baselines" / "hcp18w-approx-v1.toml"
    )
    html = TestClient(app).get("/clubs?club=iron_7").text
    assert "Streuung" in html and 'class="chart scatter"' in html and "12 Schläge" in html
    assert TestClient(app).get("/clubs?club=nonsense").status_code == 200


def test_scatter_axes_are_labelled_with_numbers_and_unit(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    insert_shots(con, [("mevo", "radar", float(i % 9) - 4.0, 100.0 + i, None) for i in range(12)])
    (group,) = scatter_groups(con, "iron_7")
    svg = scatter_svg(group, 1.0, "m")
    assert "seitlich (m)" in svg and "Länge (m)" in svg  # Einheit an beiden Achsen
    assert "&#8592; links" in svg and "rechts &#8594;" in svg
    ticks = re.findall(
        r'class="axis" x="[\d.]+" y="3[0-9.]+" text-anchor="middle">([\d.]+)</text>', svg
    )
    side = [t for t in ticks if t != "0"]
    assert ticks.count("0") == 1 and side  # Mitte einmal, Marken beidseits davon
    assert all(side.count(t) == 2 for t in side)  # jede Marke links und rechts
    yd = scatter_svg(group, 1.0936, "yd")
    assert "seitlich (yd)" in yd and "Länge (yd)" in yd


def test_every_point_has_a_large_hit_area_with_length_and_side_tooltip(tmp_path: Path) -> None:
    con = open_db(tmp_path / "t.duckdb")
    sides = [-3.5, 2.0] + [0.0] * 10
    insert_shots(con, [("mevo", "radar", side, 120.0 + i, None) for i, side in enumerate(sides)])
    (group,) = scatter_groups(con, "iron_7")
    svg = scatter_svg(group, 1.0, "m")
    assert svg.count('class="pt-hit"') == svg.count('class="pt"') == 12  # Trefferkreis je Punkt
    assert 'r="9"' in svg and 'r="3"' in svg
    assert "Länge 120 m, seitlich 3.5 m links" in svg
    assert "Länge 121 m, seitlich 2.0 m rechts" in svg
    assert "95-%-Ellipse: Mitte Länge" in svg and "Halbachsen" in svg
    yd = scatter_svg(group, 1.0936, "yd")
    assert "Länge 131 yd, seitlich 3.8 yd links" in yd  # 120 m und 3,5 m umgerechnet


def test_scatter_points_do_not_reuse_the_trend_chart_dot_style(tmp_path: Path) -> None:
    """`.dot` zeichnet im Differential-Diagramm einen Kreisrand; die Streuung erbt ihn nicht."""
    con = open_db(tmp_path / "t.duckdb")
    insert_shots(con, [("mevo", "radar", float(i % 5), 100.0 + i, None) for i in range(12)])
    (group,) = scatter_groups(con, "iron_7")
    assert 'class="dot"' not in scatter_svg(group, 1.0, "m")
