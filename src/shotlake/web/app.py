"""Weboberfläche (FastAPI + Jinja, serverseitig gerendert, ohne Build-Schritt)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import duckdb
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..analytics.course_shots import club_distances
from ..analytics.distance import club_stats
from ..analytics.score import round_scores
from ..analytics.sg import CATEGORIES, strokes_gained
from ..analytics.trend import differential_trend
from ..baseline import Baseline, load_baseline
from ..config import sync_club_map
from ..db import open_db
from ..service import (
    default_source,
    round_detail,
    round_sources,
    set_round_inputs,
    source_statuses,
)
from .charts import bars_svg, trend_svg

HERE = Path(__file__).parent
M_TO_YD = 1 / 0.9144
NA = "\N{EN DASH}"  # fehlender Wert (nie als 0 darstellen)
SG_NAMES = {
    "tee": "Abschlag",
    "approach": "Annäherung",
    "short": "Kurzes Spiel",
    "green_play": "Aufs Grün + Putts",
    "putting": "Putten",
    "penalty": "Strafschläge",
}


def create_app(data_dir: Path, club_map: Path, baseline_path: Path) -> FastAPI:
    app = FastAPI(title="Shotlake", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    lock = threading.Lock()  # DuckDB-Datei: Zugriffe nacheinander, Verbindung je Anfrage
    baseline: Baseline = load_baseline(baseline_path)

    @contextmanager
    def session() -> Iterator[duckdb.DuckDBPyConnection]:
        with lock:
            con = open_db(data_dir / "shotlake.duckdb")
            try:
                sync_club_map(con, club_map)
                yield con
            finally:
                con.close()

    def unit_of(request: Request) -> str:
        return "yd" if request.query_params.get("u") == "yd" else "m"

    def dist(value_m: float | None, unit: str, digits: int = 0) -> str:
        if value_m is None:
            return NA
        v = value_m * M_TO_YD if unit == "yd" else value_m
        return f"{v:.{digits}f}"

    def num(value: float | None, digits: int = 1, sign: bool = False) -> str:
        if value is None:
            return NA
        return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"

    def pct(value: float | None) -> str:
        return NA if value is None else f"{value:.0%}"

    def link(request: Request, path: str, **changes: str | None) -> str:
        params = {k: v for k, v in request.query_params.items() if k in ("u", "source", "w")}
        for k, v in changes.items():
            if v is None:
                params.pop(k, None)
            else:
                params[k] = v
        return path + (f"?{urlencode(params)}" if params else "")

    templates.env.filters.update(dist=dist, num=num, pct=pct, urlq=lambda s: quote(s, safe=""))
    templates.env.globals.update(link=link, sg_names=SG_NAMES)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        unit = unit_of(request)
        return templates.TemplateResponse(
            request, name, {"unit": unit, "baseline": baseline, "request": request, **ctx}
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with session() as con:
            sources = round_sources(con)
            source = request.query_params.get("source") or default_source(con)
            window = int(request.query_params.get("w", "5") or 5)
            scores = round_scores(con, source=source) if source else []
            trend = differential_trend(scores, max(window, 1))
            sg = strokes_gained(con, baseline, source=source) if source else None
            totals = sg.totals() if sg else {}
            bars = bars_svg(
                [
                    (SG_NAMES[c], totals[c] / sg.complete_holes)
                    for c in CATEGORIES
                    if sg and sg.complete_holes and (totals[c] != 0 or c != "green_play")
                ],
                "/Loch",
            )
            return page(
                request,
                "dashboard.html",
                source=source,
                sources=sources,
                window=window,
                trend=trend,
                trend_svg=trend_svg(trend, window),
                n_scores=len(scores),
                recent=list(reversed(scores))[:8],
                sg=sg,
                sg_totals=totals,
                sg_total=sum(h.total or 0.0 for h in sg.holes if h.complete) if sg else 0.0,
                bars=bars,
                statuses=source_statuses(con),
            )

    @app.get("/rounds", response_class=HTMLResponse)
    def rounds(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or None
            only_relevant = request.query_params.get("relevant") == "1"
            rows = round_scores(con, source=source, only_relevant=only_relevant)
            return page(
                request,
                "rounds.html",
                source=source,
                sources=round_sources(con),
                rows=list(reversed(rows)),
                only_relevant=only_relevant,
            )

    @app.get("/rounds/{round_id:path}", response_class=HTMLResponse)
    def round_page(request: Request, round_id: str) -> HTMLResponse:
        with session() as con:
            detail = round_detail(con, round_id, baseline)
            if detail is None:
                raise HTTPException(404, "Runde nicht gefunden")
            return page(request, "round.html", d=detail, errors=[], saved=False)

    @app.post("/rounds/{round_id:path}")
    def round_inputs(
        request: Request,
        round_id: str,
        relevant: Annotated[str, Form()] = "auto",
        hi: Annotated[str, Form()] = "",
        pcc: Annotated[str, Form()] = "",
        ags: Annotated[str, Form()] = "",
    ) -> Any:
        with session() as con:
            detail = round_detail(con, round_id, baseline)
            if detail is None:
                raise HTTPException(404, "Runde nicht gefunden")
            errors = set_round_inputs(
                con, detail.score.round_id, relevant=relevant, hi=hi, pcc=pcc, ags=ags
            )
            if errors:
                response = page(request, "round.html", d=detail, errors=errors, saved=False)
                response.status_code = 400
                return response
        return RedirectResponse(
            link(request, f"/rounds/{quote(detail.score.round_id, safe='|')}") + "#eingaben",
            status_code=303,
        )

    @app.get("/clubs", response_class=HTMLResponse)
    def clubs(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or default_source(con)
            return page(
                request,
                "clubs.html",
                source=source,
                sources=round_sources(con),
                course=club_distances(con, min_n=5, source=source) if source else [],
                range_stats=club_stats(con, min_n=10),
            )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request) -> HTMLResponse:
        with session() as con:
            runs = con.execute(
                "select started_at, source, filename, status, sessions_new, shots_new, "
                "shots_changed, shots_unchanged, errors from import_run "
                "order by started_at desc limit 20"
            ).fetchall()
            return page(request, "sources.html", statuses=source_statuses(con), runs=runs)

    return app
