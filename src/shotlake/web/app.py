"""Weboberfläche (FastAPI + Jinja, serverseitig gerendert, ohne Build-Schritt)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import reports
from ..analytics.course_shots import club_distances
from ..analytics.distance import club_stats
from ..analytics.insights import generate as generate_insights
from ..analytics.metrics import METRICS
from ..analytics.score import round_scores
from ..analytics.sg import CATEGORIES, strokes_gained
from ..analytics.trend import differential_trend
from ..baseline import Baseline
from ..clubs import CANONICAL_CLUBS
from ..service import (
    default_source,
    round_detail,
    round_sources,
    set_round_inputs,
    set_tee_values,
    source_statuses,
)
from ..workspace import Workspace
from .charts import Marker, bars_svg, trend_svg

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
    workspace = Workspace(data_dir, club_map, baseline_path)
    session = workspace.session
    baseline: Baseline = workspace.baseline

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
            notes = [
                n
                for n in reports.list_notes(con, limit=500)
                if n["scope"] == "global" or n["scope"] == f"source:{source}"
            ]
            markers = [
                Marker(
                    date.fromisoformat(n["date"]),
                    n["text"][:80],
                    n["type"],
                    link(request, "/compare", at=n["date"], source=source),
                )
                for n in notes
            ]
            first = trend[0].start_utc.date() if trend else None
            last = trend[-1].start_utc.date() if trend else None
            outside = sum(
                1 for m in markers if first is None or not first <= m.when <= (last or m.when)
            )
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
                trend_svg=trend_svg(trend, window, markers),
                n_markers=len(markers) - outside,
                n_markers_outside=outside,
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
            return page(
                request,
                "round.html",
                d=detail,
                errors=[],
                tee_errors=[],
                round_notes=reports.list_notes(con, scope=f"round:{detail.score.round_id}"),
                today=date.today().isoformat(),
            )

    def back_to_round(request: Request, round_id: str, anchor: str) -> RedirectResponse:
        target = link(request, f"/rounds/{quote(round_id, safe='|')}") + anchor
        return RedirectResponse(target, status_code=303)

    @app.post("/rounds/{round_id:path}/tee")
    def round_tee(
        request: Request,
        round_id: str,
        all_rating: Annotated[str, Form()] = "",
        all_slope: Annotated[str, Form()] = "",
        front9_rating: Annotated[str, Form()] = "",
        front9_slope: Annotated[str, Form()] = "",
        back9_rating: Annotated[str, Form()] = "",
        back9_slope: Annotated[str, Form()] = "",
    ) -> Any:
        with session() as con:
            detail = round_detail(con, round_id, baseline)
            if detail is None:
                raise HTTPException(404, "Runde nicht gefunden")
            errors = set_tee_values(
                con,
                detail.score.round_id,
                {
                    "all": (all_rating, all_slope),
                    "front9": (front9_rating, front9_slope),
                    "back9": (back9_rating, back9_slope),
                },
            )
            if errors:
                response = page(request, "round.html", d=detail, errors=[], tee_errors=errors)
                response.status_code = 400
                return response
        return back_to_round(request, detail.score.round_id, "#platzwerte")

    @app.post("/rounds/{round_id:path}/relevant")
    def round_relevant(
        request: Request,
        round_id: str,
        relevant: Annotated[str, Form()],
        back: Annotated[str, Form()] = "",
    ) -> Any:
        with session() as con:
            detail = round_detail(con, round_id, baseline)
            if detail is None:
                raise HTTPException(404, "Runde nicht gefunden")
            if relevant not in ("yes", "no"):
                raise HTTPException(400, "ungültiger Wert")
            con.execute(
                "insert or ignore into round_input (round_id, updated_at) "
                "values (?, now()::timestamp)",
                [detail.score.round_id],
            )
            con.execute(
                "update round_input set handicap_relevant = ?, updated_at = now()::timestamp "
                "where round_id = ?",
                [relevant == "yes", detail.score.round_id],
            )
        # Rücksprung nur auf lokale Pfade (kein offener Redirect)
        safe = back if back.startswith("/") and not back.startswith("//") else "/rounds"
        return RedirectResponse(safe, status_code=303)

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
                response = page(request, "round.html", d=detail, errors=errors, tee_errors=[])
                response.status_code = 400
                return response
        return back_to_round(request, detail.score.round_id, "#eingaben")

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

    @app.get("/insights", response_class=HTMLResponse)
    def insights(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or None
            uncertain = request.query_params.get("uncertain") == "1"
            found = generate_insights(con, baseline, source=source, include_uncertain=uncertain)
            return page(
                request,
                "insights.html",
                source=source,
                sources=round_sources(con),
                insights=found,
                uncertain=uncertain,
            )

    # --- Notizen -----------------------------------------------------------------------------

    def note_form_values(
        text: str, type_: str, note_date: str, valid_to: str, scope_kind: str, scope_value: str
    ) -> tuple[dict[str, str], str]:
        scope = "global" if scope_kind == "global" else f"{scope_kind}:{scope_value.strip()}"
        values = {
            "text": text, "type": type_, "date": note_date, "valid_to": valid_to,
            "scope_kind": scope_kind, "scope_value": scope_value,
        }  # fmt: skip
        return values, scope

    def notes_context(con: Any, request: Request) -> dict[str, Any]:
        type_ = request.query_params.get("type") or None
        return {
            "notes": reports.list_notes(con, type_=type_, limit=300),
            "filter_type": type_,
            "note_types": reports.NOTE_TYPES,
            "clubs": CANONICAL_CLUBS,
            "sources": sorted(reports.known_sources(con)),
        }

    def safe_back(back: str, default: str) -> str:
        return back if back.startswith("/") and not back.startswith("//") else default

    @app.get("/notes", response_class=HTMLResponse)
    def notes(request: Request) -> HTMLResponse:
        with session() as con:
            return page(
                request,
                "notes.html",
                errors=[],
                form=None,
                today=date.today().isoformat(),
                **notes_context(con, request),
            )

    @app.post("/notes")
    def note_create(
        request: Request,
        text: Annotated[str, Form()] = "",
        type: Annotated[str, Form()] = "general",
        date_: Annotated[str, Form(alias="date")] = "",
        valid_to: Annotated[str, Form()] = "",
        scope_kind: Annotated[str, Form()] = "global",
        scope_value: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
    ) -> Any:
        values, scope = note_form_values(text, type, date_, valid_to, scope_kind, scope_value)
        with session() as con:
            try:
                reports.add_note(con, text, type, date_, scope, valid_to or None)
            except ValueError as exc:
                response = page(
                    request,
                    "notes.html",
                    errors=str(exc).split("; "),
                    form=values,
                    today=date.today().isoformat(),
                    **notes_context(con, request),
                )
                response.status_code = 400
                return response
        return RedirectResponse(safe_back(back, link(request, "/notes")), status_code=303)

    @app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
    def note_edit(request: Request, note_id: str) -> HTMLResponse:
        with session() as con:
            note = reports.get_note(con, note_id)
            if note is None:
                raise HTTPException(404, "Notiz nicht gefunden")
            kind, _, value = note["scope"].partition(":")
            form = {
                "text": note["text"], "type": note["type"], "date": note["date"],
                "valid_to": note["valid_to"] or "", "scope_kind": kind, "scope_value": value,
            }  # fmt: skip
            return page(request, "note_edit.html", note=note, form=form, errors=[],
                        **notes_context(con, request))  # fmt: skip

    @app.post("/notes/{note_id}/edit")
    def note_update(
        request: Request,
        note_id: str,
        text: Annotated[str, Form()] = "",
        type: Annotated[str, Form()] = "general",
        date_: Annotated[str, Form(alias="date")] = "",
        valid_to: Annotated[str, Form()] = "",
        scope_kind: Annotated[str, Form()] = "global",
        scope_value: Annotated[str, Form()] = "",
    ) -> Any:
        values, scope = note_form_values(text, type, date_, valid_to, scope_kind, scope_value)
        with session() as con:
            note = reports.get_note(con, note_id)
            if note is None:
                raise HTTPException(404, "Notiz nicht gefunden")
            try:
                reports.update_note(con, note_id, text, type, date_, scope, valid_to or None)
            except ValueError as exc:
                response = page(
                    request, "note_edit.html", note=note, form=values,
                    errors=str(exc).split("; "), **notes_context(con, request),
                )  # fmt: skip
                response.status_code = 400
                return response
        return RedirectResponse(link(request, "/notes"), status_code=303)

    @app.get("/notes/{note_id}/delete", response_class=HTMLResponse)
    def note_delete_confirm(request: Request, note_id: str) -> HTMLResponse:
        with session() as con:
            note = reports.get_note(con, note_id)
            if note is None:
                raise HTTPException(404, "Notiz nicht gefunden")
            return page(request, "note_delete.html", note=note)

    @app.post("/notes/{note_id}/delete")
    def note_delete(request: Request, note_id: str) -> Any:
        with session() as con:
            if not reports.delete_note(con, note_id):
                raise HTTPException(404, "Notiz nicht gefunden")
        return RedirectResponse(link(request, "/notes"), status_code=303)

    # --- Vergleich A/B und Vorher/Nachher ---------------------------------------------------------

    compare_default = (
        "to_par_per_hole", "putts_per_hole", "fir_rate", "gir_rate", "sg_total_per_hole",
        "differential",
    )  # fmt: skip

    @app.get("/compare", response_class=HTMLResponse)
    def compare(request: Request) -> HTMLResponse:
        q = request.query_params
        with session() as con:
            source = q.get("source") or default_source(con)
            at = q.get("at") or ""
            errors: list[str] = []
            a: dict[str, Any] = {
                "since": q.get("a_since") or None,
                "until": q.get("a_until") or None,
            }
            b: dict[str, Any] = {
                "since": q.get("b_since") or None,
                "until": q.get("b_until") or None,
            }
            if at:
                try:
                    day = date.fromisoformat(at)
                    a = {"since": day.isoformat(), "until": None}
                    b = {"since": None, "until": (day - timedelta(days=1)).isoformat()}
                except ValueError:
                    errors.append("at: Datum im Format JJJJ-MM-TT erwartet")
            metrics = [m for m in q.getlist("metric") if m in METRICS] or list(compare_default)
            result = None
            if not errors and (a["since"] or a["until"]) and (b["since"] or b["until"]):
                try:
                    result = reports.compare_report(
                        con, baseline, a, b, metrics,
                        reports.make_filters(source=source),
                    )  # fmt: skip
                except ValueError as exc:
                    errors.append(str(exc))
            return page(
                request,
                "compare.html",
                source=source,
                sources=round_sources(con),
                at=at,
                a=a,
                b=b,
                result=result,
                errors=errors,
                metrics=metrics,
                all_metrics=METRICS,
                notes=[n for n in reports.list_notes(con, limit=100) if n["date"] == at],
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
