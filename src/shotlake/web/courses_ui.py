"""Platz-Stammdaten in der Weboberfläche: Stammplätze pflegen, Plätze der Quellen zuordnen.

Eingaben werden geprüft (`courses.CourseError`); Meldungen erscheinen oben auf der Seite. Alle
Änderungen sind POST mit Weiterleitung auf die Seite (kein Zustand in der Adresse).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated
from urllib.parse import quote

import duckdb
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import courses
from ..courses import CourseError

PART_LABELS = {"all": "18 Löcher", "front9": "vordere 9", "back9": "hintere 9"}


def _number(text: str, label: str) -> float | None:
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise CourseError(f"{label}: {text!r} ist keine Zahl") from None


def _integer(text: str, label: str) -> int | None:
    value = _number(text, label)
    if value is None:
        return None
    if value != int(value):
        raise CourseError(f"{label}: ganze Zahl erwartet")
    return int(value)


def install_courses(
    app: FastAPI,
    session: Callable[[], AbstractContextManager[duckdb.DuckDBPyConnection]],
    page: Callable[..., HTMLResponse],
    link: Callable[..., str],
    donors: Callable[[duckdb.DuckDBPyConnection], dict[str, tuple[float, int, str]]],
) -> None:
    workspace_donors = donors

    def back(request: Request, path: str, message: str | None = None) -> RedirectResponse:
        target = link(request, path)
        if message:
            target += ("&" if "?" in target else "?") + f"msg={quote(message)}"
        return RedirectResponse(target, status_code=303)

    @app.get("/courses", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        with session() as con:
            masters = courses.list_masters(con)
            sources = courses.source_courses(con, workspace_donors(con))
            linked: dict[str, int] = {}
            for c in sources:
                if c.master:
                    linked[c.master.master_id] = linked.get(c.master.master_id, 0) + 1
            return page(
                request, "courses.html", masters=masters, sources=sources, linked=linked,
                part_labels=PART_LABELS,
            )  # fmt: skip

    @app.post("/courses")
    def create(request: Request, name: Annotated[str, Form()] = "") -> Response:
        with session() as con:
            try:
                master_id = courses.save_master(con, name=name)
            except CourseError as exc:
                return back(request, "/courses", str(exc))
        return back(request, f"/courses/{master_id}", "Stammplatz angelegt: Tees ergänzen")

    @app.post("/courses/import")
    def import_course(request: Request, course_id: Annotated[str, Form()] = "") -> Response:
        with session() as con:
            try:
                master_id = courses.import_from_course(con, course_id)
            except CourseError as exc:
                return back(request, "/courses", str(exc))
        return back(request, f"/courses/{master_id}", "Aus der Quelle übernommen: bitte prüfen")

    @app.post("/courses/link")
    def link_course(
        request: Request,
        course_id: Annotated[str, Form()] = "",
        master: Annotated[str, Form()] = "auto",
    ) -> Response:
        with session() as con:
            try:
                if master == "auto":
                    courses.unlink_course(con, course_id)
                else:
                    courses.link_course(con, course_id, None if master == "none" else master)
            except CourseError as exc:
                return back(request, "/courses", str(exc))
        return back(request, "/courses", "Zuordnung gespeichert")

    @app.get("/courses/{master_id}", response_class=HTMLResponse)
    def edit(request: Request, master_id: str) -> Response:
        with session() as con:
            master = courses.get_master(con, master_id)
            if master is None:
                return back(request, "/courses", "Stammplatz nicht gefunden")
            matched = [
                c
                for c in courses.source_courses(con, workspace_donors(con))
                if c.master and c.master.master_id == master_id
            ]
            return page(
                request, "course_edit.html", master=master, matched=matched,
                part_labels=PART_LABELS,
            )  # fmt: skip

    @app.post("/courses/{master_id}")
    def update(
        request: Request,
        master_id: str,
        name: Annotated[str, Form()] = "",
        city: Annotated[str, Form()] = "",
        country: Annotated[str, Form()] = "",
        lat: Annotated[str, Form()] = "",
        lon: Annotated[str, Form()] = "",
        par_total: Annotated[str, Form()] = "",
        default_tee: Annotated[str, Form()] = "",
        aliases: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
    ) -> Response:
        with session() as con:
            try:
                courses.save_master(
                    con, master_id=master_id, name=name, city=city, country=country,
                    lat=_number(lat, "Breitengrad"), lon=_number(lon, "Längengrad"),
                    par_total=_integer(par_total, "Par"), default_tee=default_tee,
                    aliases=[a for a in aliases.split(",")], note=note,
                )  # fmt: skip
            except CourseError as exc:
                return back(request, f"/courses/{master_id}", str(exc))
        return back(request, f"/courses/{master_id}", "Gespeichert")

    @app.post("/courses/{master_id}/tee")
    def add_tee(
        request: Request,
        master_id: str,
        tee_name: Annotated[str, Form()] = "",
        part: Annotated[str, Form()] = "all",
        rating: Annotated[str, Form()] = "",
        slope: Annotated[str, Form()] = "",
        par: Annotated[str, Form()] = "",
    ) -> Response:
        path = f"/courses/{master_id}"
        with session() as con:
            try:
                value = _number(rating, "Course Rating")
                slope_value = _integer(slope, "Slope")
                if value is None or slope_value is None:
                    raise CourseError("Course Rating und Slope angeben")
                courses.set_tee(
                    con, master_id, tee_name, rating=value, slope=slope_value, part=part,
                    par=_integer(par, "Par"),
                )  # fmt: skip
            except CourseError as exc:
                return back(request, path, str(exc))
        return back(request, path, f"Tee {tee_name.strip()} gespeichert")

    @app.post("/courses/{master_id}/tee/delete")
    def delete_tee(
        request: Request,
        master_id: str,
        tee_name: Annotated[str, Form()] = "",
        part: Annotated[str, Form()] = "",
    ) -> Response:
        with session() as con:
            n = courses.delete_tee(con, master_id, tee_name, part or None)
        return back(request, f"/courses/{master_id}", f"{n} Eintrag entfernt")

    @app.post("/courses/{master_id}/delete")
    def delete(request: Request, master_id: str, confirm: Annotated[str, Form()] = "") -> Response:
        if confirm != "yes":
            return back(request, f"/courses/{master_id}", "Nicht bestätigt: nichts gelöscht")
        with session() as con:
            try:
                courses.delete_master(con, master_id)
            except CourseError as exc:
                return back(request, "/courses", str(exc))
        return back(
            request, "/courses", "Stammplatz gelöscht (Runden und Quellen bleiben unberührt)"
        )
