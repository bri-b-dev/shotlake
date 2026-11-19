"""Weboberfläche (FastAPI + Jinja, serverseitig gerendert, ohne Build-Schritt)."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, timedelta
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import dedup as dedup_mod
from .. import reports
from ..analytics.course_shots import club_distances
from ..analytics.dispersion import scatter_groups
from ..analytics.distance import DEFAULT_MIN_SPEED_RATIO, club_stats
from ..analytics.insights import generate as generate_insights
from ..analytics.metrics import METRICS
from ..analytics.score import round_scores
from ..analytics.sg import CATEGORIES, strokes_gained
from ..analytics.trend import differential_trend
from ..baseline import Baseline
from ..clubs import CANONICAL_CLUBS, club_sort_key
from ..connectors.api import ApiConnector
from ..connectors.registry import API_CONNECTORS
from ..secret_store import CREDENTIAL_FIELDS, SecretStoreError
from ..service import (
    round_detail,
    round_sources,
    set_round_inputs,
    set_tee_values,
    source_statuses,
)
from ..workspace import Workspace
from .auth import install_auth
from .backup_ui import install_backup
from .charts import Marker, bars_svg, box_row, boxplot_svg, ci_svg, scatter_svg, trend_svg
from .sync import install_settings

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


def _version() -> str:
    try:
        return metadata.version("shotlake")
    except metadata.PackageNotFoundError:
        return "unbekannt"


def _improved(insight: Any) -> bool | None:
    """Veränderungen: `besser` oder `schlechter` bestimmt die Farbe, nicht das Vorzeichen."""
    if insight.kind != "trend":
        return None
    return "wahrscheinlich besser" in (insight.headline or insight.text)


def create_app(
    data_dir: Path,
    club_map: Path,
    baseline_path: Path,
    dedup_config: Path | None = None,
    auth: bool | None = None,
    connector_factory: Callable[[str], ApiConnector] | None = None,
    garmin_factory: Callable[[str | None, str | None, bool], Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="Shotlake", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    workspace = Workspace(data_dir, club_map, baseline_path, dedup_config)
    session = workspace.session
    baseline: Baseline = workspace.baseline
    choices: dict[str, Baseline] = {b.id: b for b in workspace.available_baselines()}

    def base_of(request: Request) -> Baseline:
        """Baseline der Anfrage (`?b=<ID>`); fehlt sie oder ist unbekannt: der Standard."""
        return choices.get(request.query_params.get("b", ""), baseline)

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
        params = {
            k: v for k, v in request.query_params.items() if k in ("u", "source", "w", "club")
        }
        if request.query_params.get("b") in choices:  # nur gültige Kennungen weitergeben
            params["b"] = request.query_params["b"]
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
            request,
            name,
            {
                "unit": unit,
                "baseline": base_of(request),
                "baselines": list(choices.values()),
                "request": request,
                **ctx,
            },
        )

    if auth is None:  # `SHOTLAKE_WEB_AUTH=off` nur für Betrieb hinter eigenem Schutz
        auth = os.environ.get("SHOTLAKE_WEB_AUTH", "on").lower() != "off"

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: der Prozess antwortet (ohne Anmeldung, ohne Datenbankzugriff)."""
        return {"status": "ok", "version": _version()}

    @app.get("/readyz")
    def readyz() -> Response:
        """Readiness: Datenverzeichnis beschreibbar und Datenbank lesbar (ohne Anmeldung)."""
        try:
            if not os.access(data_dir, os.W_OK):
                raise OSError("Datenverzeichnis nicht beschreibbar")
            state = workspace.ping()
        except Exception as exc:  # jede Störung heißt: nicht bereit, ohne Details nach außen
            return JSONResponse({"status": "unavailable", "error": type(exc).__name__}, 503)
        return JSONResponse({"status": "ok", "db": state, "version": _version()})

    auth_store = install_auth(app, data_dir, page, enabled=auth)
    sync_manager = install_settings(
        app, workspace, auth_store, page, connector_factory, garmin_factory
    )
    install_backup(app, workspace, auth_store)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with session() as con:
            sources = round_sources(con)
            source = request.query_params.get("source") or None  # leer = alle, dedupliziert
            scope = workspace.scope(con)
            window = int(request.query_params.get("w", "5") or 5)
            scores = round_scores(
                con,
                source=source,
                exclude=None if source else scope.exclude("score"),
                donors=scope.donors,
            )
            trend = differential_trend(scores, max(window, 1))
            notes = [
                n
                for n in reports.list_notes(con, limit=500)
                if n["scope"] == "global" or (source and n["scope"] == f"source:{source}")
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
            sg = strokes_gained(
                con,
                base_of(request),
                source=source,
                exclude_rounds=None if source else scope.exclude("shots"),
            )
            totals = sg.totals() if sg else {}
            sg_items = [
                (SG_NAMES[c], totals[c] / sg.complete_holes)
                for c in CATEGORIES
                if sg and sg.complete_holes and (totals[c] != 0 or c != "green_play")
            ]
            bars = bars_svg(sg_items, "/Loch")
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
                proposals=dedup_mod.open_proposals(con),
                recent=list(reversed(scores))[:8],
                sg=sg,
                sg_totals=totals,
                sg_total=sum(h.total or 0.0 for h in sg.holes if h.complete) if sg else 0.0,
                bars=bars,
                sg_worst=min(sg_items, key=lambda i: i[1]) if sg_items else None,
                sg_best=max(sg_items, key=lambda i: i[1]) if sg_items else None,
                statuses=source_statuses(con),
            )

    @app.get("/rounds", response_class=HTMLResponse)
    def rounds(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or None
            only_relevant = request.query_params.get("relevant") == "1"
            show_dups = request.query_params.get("dups") == "1"
            scope = workspace.scope(con)
            rows = round_scores(
                con,
                source=source,
                only_relevant=only_relevant,
                exclude=None if source or show_dups else scope.exclude("score"),
                donors=scope.donors,
            )
            partners = {
                f.round_id: sorted(m.source for m in group)
                for group in dedup_mod.cluster_facts(con)
                for f in group
            }
            return page(
                request,
                "rounds.html",
                source=source,
                sources=round_sources(con),
                rows=list(reversed(rows)),
                only_relevant=only_relevant,
                show_dups=show_dups,
                partners=partners,
                proposals=dedup_mod.open_proposals(con),
            )

    def round_dedup(con: Any, round_id: str) -> dict[str, Any]:
        """Dedup-Abschnitt der Rundenseite: Verknüpfungen, Gruppe, führende Quelle je Feldgruppe."""
        links = dedup_mod.links_of(con, round_id)
        group = next(
            (g for g in dedup_mod.cluster_facts(con) if any(f.round_id == round_id for f in g)),
            None,
        )
        scores = {f.round_id: f.gross for f in group} if group else {}
        return {
            "dedup_links": [m for m in links if m.status != "rejected"],
            "dedup_rejected": [m for m in links if m.status == "rejected"],
            "dedup_group": group,
            "dedup_leaders": dedup_mod.leaders_of(group, workspace.dedup_cfg) if group else None,
            "dedup_conflict": len({v for v in scores.values() if v is not None}) > 1,
        }

    @app.get("/rounds/{round_id:path}", response_class=HTMLResponse)
    def round_page(request: Request, round_id: str) -> HTMLResponse:
        with session() as con:
            detail = round_detail(con, round_id, base_of(request))
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
                **round_dedup(con, detail.score.round_id),
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
            detail = round_detail(con, round_id, base_of(request))
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
                response = page(
                    request,
                    "round.html",
                    d=detail,
                    errors=[],
                    tee_errors=errors,
                    **round_dedup(con, detail.score.round_id),
                )
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
            detail = round_detail(con, round_id, base_of(request))
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
            detail = round_detail(con, round_id, base_of(request))
            if detail is None:
                raise HTTPException(404, "Runde nicht gefunden")
            errors = set_round_inputs(
                con, detail.score.round_id, relevant=relevant, hi=hi, pcc=pcc, ags=ags
            )
            if errors:
                response = page(
                    request,
                    "round.html",
                    d=detail,
                    errors=errors,
                    tee_errors=[],
                    **round_dedup(con, detail.score.round_id),
                )
                response.status_code = 400
                return response
        return back_to_round(request, detail.score.round_id, "#eingaben")

    @app.get("/clubs", response_class=HTMLResponse)
    def clubs(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or None
            scope = workspace.scope(con)
            scatter_clubs = [
                r[0]
                for r in con.execute(
                    "select club_type from shot where club_type is not null and "
                    "carry_m is not null and carry_side_m is not null "
                    "group by club_type order by count(*) desc"
                ).fetchall()
            ]
            scatter_clubs.sort(key=club_sort_key)
            club = request.query_params.get("club")
            if club not in scatter_clubs:
                club = scatter_clubs[0] if scatter_clubs else None
            factor = M_TO_YD if unit_of(request) == "yd" else 1.0
            course = club_distances(
                con,
                min_n=5,
                source=source,
                exclude_rounds=None if source else scope.exclude("shots"),
            )
            plots = [
                (g, scatter_svg(g, factor, unit_of(request)))
                for g in (
                    scatter_groups(con, club, min_speed_ratio=DEFAULT_MIN_SPEED_RATIO)
                    if club
                    else []
                )
            ]
            box_rows = []
            for c in sorted(course, key=lambda c: club_sort_key(c.club)):
                if c.club == "putter" or not c.values:
                    continue
                if c.p25_m is None or c.median_m is None or c.p75_m is None:
                    continue
                box_rows.append(
                    box_row(c.club, c.values, c.p25_m, c.median_m, c.p75_m, ok=c.warning is None)
                )
            return page(
                request,
                "clubs.html",
                scatter_clubs=scatter_clubs,
                scatter_club=club,
                plots=plots,
                source=source,
                sources=round_sources(con),
                course=course,
                boxplot=boxplot_svg(box_rows, factor, unit_of(request)),
                range_stats=club_stats(con, min_n=10, min_speed_ratio=DEFAULT_MIN_SPEED_RATIO),
            )

    @app.get("/insights", response_class=HTMLResponse)
    def insights(request: Request) -> HTMLResponse:
        with session() as con:
            source = request.query_params.get("source") or None
            uncertain = request.query_params.get("uncertain") == "1"
            found = generate_insights(
                con,
                base_of(request),
                source=source,
                include_uncertain=uncertain,
                scope=workspace.scope(con),
            )
            bounds: dict[str | None, float] = {}
            for i in found:
                if i.ci is not None:
                    reach = max(abs(i.ci.low), abs(i.ci.high), abs(i.effect or 0.0))
                    bounds[i.unit] = max(bounds.get(i.unit, 0.0), reach)
            strips = {
                i.id: ci_svg(
                    i.effect or 0.0,
                    i.ci.low,
                    i.ci.high,
                    bounds[i.unit] * 1.05,
                    good=_improved(i),
                )
                for i in found
                if i.ci is not None
            }
            return page(
                request,
                "insights.html",
                improved={i.id: _improved(i) for i in found},
                source=source,
                sources=round_sources(con),
                insights=found,
                strips=strips,
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
            source = q.get("source") or None
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
                        con, base_of(request), a, b, metrics,
                        reports.make_filters(source=source),
                        scope=workspace.scope(con),
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

    # --- Dedup ----------------------------------------------------------------------------------

    def dedup_redirect(request: Request, message: str = "") -> RedirectResponse:
        target = link(request, "/dedup")
        if message:
            target += ("&" if "?" in target else "?") + urlencode({"msg": message})
        return RedirectResponse(target, status_code=303)

    @app.get("/dedup", response_class=HTMLResponse)
    def dedup_page(request: Request) -> HTMLResponse:
        with session() as con:
            matches = dedup_mod.list_matches(con)
            groups = dedup_mod.cluster_facts(con)
            linked_ids = {f.round_id for g in groups for f in g}
            all_rounds = con.execute(
                "select r.round_id, r.source, r.start_utc, r.holes_played, r.gross_strokes "
                "from round r where not r.in_progress order by r.start_utc desc limit 300"
            ).fetchall()
            return page(
                request,
                "dedup.html",
                proposals=[m for m in matches if m.status == "proposed"],
                rejected=[m for m in matches if m.status == "rejected"],
                groups=[(g, dedup_mod.leaders_of(g, workspace.dedup_cfg)) for g in groups],
                n_rounds=len(all_rounds),
                n_unlinked=sum(1 for r in all_rounds if r[0] not in linked_ids),
                round_choices=all_rounds,
                cfg=workspace.dedup_cfg,
                msg=request.query_params.get("msg", ""),
            )

    @app.post("/dedup/run")
    def dedup_run(request: Request) -> RedirectResponse:
        with session() as con:
            report = workspace.run_dedup(con)
        return dedup_redirect(request, report.as_text())

    @app.post("/dedup/accept-all")
    def dedup_accept_all(request: Request) -> RedirectResponse:
        with session() as con:
            done, skipped = dedup_mod.accept_proposed(con)
        suffix = f"; {len(skipped)} übersprungen (Konflikt)" if skipped else ""
        return dedup_redirect(request, f"{done} Vorschläge bestätigt{suffix}")

    def dedup_decision(request: Request, action: str, a: str, b: str) -> RedirectResponse:
        with session() as con:
            try:
                ra, rb = dedup_mod.resolve(con, a), dedup_mod.resolve(con, b)
                (dedup_mod.confirm if action == "confirm" else dedup_mod.reject)(con, ra, rb)
            except ValueError as exc:
                return dedup_redirect(request, f"Fehler: {exc}")
        return dedup_redirect(request, "bestätigt" if action == "confirm" else "getrennt")

    @app.post("/dedup/confirm")
    def dedup_confirm(
        request: Request, a: Annotated[str, Form()], b: Annotated[str, Form()]
    ) -> RedirectResponse:
        return dedup_decision(request, "confirm", a, b)

    @app.post("/dedup/reject")
    def dedup_reject(
        request: Request, a: Annotated[str, Form()], b: Annotated[str, Form()]
    ) -> RedirectResponse:
        return dedup_decision(request, "reject", a, b)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request) -> HTMLResponse:
        with session() as con:
            runs = con.execute(
                "select started_at, source, filename, status, sessions_new, shots_new, "
                "shots_changed, shots_unchanged, errors from import_run "
                "order by started_at desc limit 20"
            ).fetchall()
            try:
                cred_status = workspace.secrets.status()
            except SecretStoreError:
                cred_status = {}
            needs = {
                src: all(f.key in cred_status.get(src, {}) for f in fields)
                for src, fields in CREDENTIAL_FIELDS.items()
            }
            return page(
                request,
                "sources.html",
                statuses=source_statuses(con),
                runs=runs,
                api_sources=set(API_CONNECTORS),
                creds_ready=needs,
                states=sync_manager.states,
                notice=sync_manager.notice,
                running=sync_manager.running(),
            )

    return app
