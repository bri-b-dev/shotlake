"""Syncs und Datei-Uploads aus der Weboberfläche; Zugangsdaten-Pflege.

Syncs laufen im Hintergrund (ein Thread je Anstoß, höchstens ein Sync gleichzeitig, siehe
`Workspace.run_sync`); der Zustand liegt im Speicher und dient nur der Anzeige. Meldungen enthalten
nie Zugangsdaten.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..connectors.api import ApiConnector, AuthError, TransientError
from ..connectors.errors import SchemaDriftError
from ..connectors.garmin_login import GarminLogin, GarminLoginError
from ..connectors.registry import API_CONNECTORS, CONNECTORS
from ..ingest import sync_location
from ..secret_store import CREDENTIAL_FIELDS, SecretStoreError
from ..workspace import Workspace
from .auth import COOKIE, MIN_PASSWORD, SESSION_DAYS, AuthStore

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class SyncState:
    state: str  # running | ok | error
    started: datetime
    mode: str = "incremental"
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SyncManager:
    def __init__(
        self, workspace: Workspace, connector_factory: Callable[[str], ApiConnector] | None
    ) -> None:
        self.workspace = workspace
        self.factory = connector_factory or workspace.make_connector
        self.states: dict[str, SyncState] = {}
        self.notice: dict[str, str] = {}
        self._guard = threading.Lock()

    def running(self) -> bool:
        return any(s.state == "running" for s in self.states.values())

    def start(self, source: str, mode: str) -> bool:
        """Sync anstoßen; False, wenn schon einer läuft."""
        with self._guard:
            if self.running():
                return False
            self.states[source] = SyncState("running", datetime.now(), mode)
        threading.Thread(target=self._run, args=(source, mode), daemon=True).start()
        return True

    def _run(self, source: str, mode: str) -> None:
        state = self.states[source]
        try:
            rep = self.workspace.run_sync(lambda: self.factory(source), mode)
            if rep is None:
                state.state, state.text = "error", "Es läuft bereits ein Sync."
            else:
                state.text = rep.as_text()
                state.warnings = rep.warnings[:10]
                state.errors = rep.errors[:10]
                state.state = "ok" if rep.status == "ok" else "error"
        except Exception as exc:  # Hintergrund-Thread: nie ohne Meldung sterben
            state.state, state.text = "error", f"Sync fehlgeschlagen ({type(exc).__name__})"
        state.started = datetime.now()

    def check(self, source: str) -> tuple[bool, str]:
        """Zugangsdaten prüfen (Anmeldung beim Dienst, ohne Daten zu importieren)."""
        try:
            self.factory(source).authenticate()
        except AuthError as exc:
            return False, f"Anmeldung abgelehnt: {str(exc)[:160]}"
        except (TransientError, SchemaDriftError) as exc:
            return False, f"Dienst nicht erreichbar oder Antwort unerwartet: {str(exc)[:160]}"
        except Exception as exc:
            return False, f"Prüfung fehlgeschlagen ({type(exc).__name__})"
        return True, "Anmeldung erfolgreich."


def parse_arccos_link(text: str) -> dict[str, str]:
    """Aus der `…/magic-link?accessKey=…&secret=…&userId=…`-URL die drei Werte lesen."""
    query = parse_qs(urlparse(text.strip()).query)
    mapping = {"accessKey": "access_key", "secret": "secret", "userId": "user_id"}
    return {dst: query[src][0] for src, dst in mapping.items() if query.get(src)}


def install_settings(
    app: FastAPI,
    workspace: Workspace,
    auth: AuthStore,
    render: Callable[..., HTMLResponse],
    connector_factory: Callable[[str], ApiConnector] | None = None,
    garmin_factory: Callable[[str | None, str | None, bool], Any] | None = None,
) -> SyncManager:
    manager = SyncManager(workspace, connector_factory)
    garmin = GarminLogin(workspace.garmin_dir, garmin_factory)
    notice = manager.notice  # letzte Meldung je Quelle (nur Anzeige)

    def back(target: str = "/sources") -> RedirectResponse:
        return RedirectResponse(target, status_code=303)

    def credentials_view() -> list[dict[str, Any]]:
        try:
            status = workspace.secrets.status()
            problem = None
        except SecretStoreError as exc:
            status, problem = {}, str(exc)
        out = []
        for source, fields in CREDENTIAL_FIELDS.items():
            out.append(
                {
                    "source": source,
                    "problem": problem,
                    "fields": [
                        {
                            "key": f.key,
                            "label": f.label,
                            "secret": f.secret,
                            "set": f.key in status.get(source, {}),
                            "updated": status.get(source, {}).get(f.key, "")[:10],
                            "value": ""
                            if problem
                            else (workspace.secrets.visible(source, f.key) or ""),
                        }
                        for f in fields
                    ],
                    "notice": notice.get(source, ""),
                }
            )
        return out

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> Response:
        return render(
            request,
            "settings.html",
            creds=credentials_view(),
            min_password=MIN_PASSWORD,
            garmin_tokens=garmin.tokens_present(),
            garmin_pending=garmin.pending(),
            garmin_notice=notice.get("garmin_platform", ""),
            message=request.query_params.get("msg", ""),
        )

    @app.post("/settings/credentials/{source}")
    async def save_credentials(request: Request, source: str) -> Response:
        if source not in CREDENTIAL_FIELDS:
            return Response("Unbekannte Quelle", status_code=404)
        form = await request.form()
        values = {f.key: str(form.get(f.key, "")) for f in CREDENTIAL_FIELDS[source]}
        link = str(form.get("magic_link", ""))
        if source == "arccos" and link.strip():
            values.update(parse_arccos_link(link))
        if not any(v.strip() for v in values.values()):
            notice[source] = "Nichts eingetragen."
            return back("/settings")
        try:
            workspace.secrets.set_many(source, values)
            notice[source] = "Gespeichert."
        except SecretStoreError as exc:
            notice[source] = str(exc)
        return back("/settings")

    @app.post("/settings/credentials/{source}/delete")
    def delete_credentials(source: str) -> Response:
        if source not in CREDENTIAL_FIELDS:
            return Response("Unbekannte Quelle", status_code=404)
        try:
            workspace.secrets.delete(source)
            notice[source] = "Gelöscht."
        except SecretStoreError as exc:
            notice[source] = str(exc)
        return back("/settings")

    @app.post("/settings/garmin/login")
    async def garmin_login(request: Request) -> Response:
        form = await request.form()
        email, password = str(form.get("email", "")), str(form.get("password", ""))
        if not email.strip() or not password:
            notice["garmin_platform"] = "E-Mail und Passwort eintragen."
            return back("/settings")
        try:
            result = await run_in_threadpool(garmin.start, email, password)
            notice["garmin_platform"] = (
                "Angemeldet, Token gespeichert."
                if result == "ok"
                else "Garmin hat einen Code geschickt: unten eintragen."
            )
        except GarminLoginError as exc:
            notice["garmin_platform"] = str(exc)
        return back("/settings")

    @app.post("/settings/garmin/code")
    async def garmin_code(request: Request) -> Response:
        form = await request.form()
        code = str(form.get("code", "")).strip()
        if not code:
            notice["garmin_platform"] = "Code eintragen."
            return back("/settings")
        try:
            await run_in_threadpool(garmin.finish, code)
            notice["garmin_platform"] = "Angemeldet, Token gespeichert."
        except GarminLoginError as exc:
            notice["garmin_platform"] = str(exc)
        return back("/settings")

    @app.post("/settings/garmin/cancel")
    def garmin_cancel() -> Response:
        garmin.cancel()
        notice["garmin_platform"] = "Abgebrochen."
        return back("/settings")

    @app.post("/settings/password")
    async def change_password(request: Request) -> Response:
        form = await request.form()
        current, new, repeat = (str(form.get(k, "")) for k in ("current", "new", "repeat"))
        if not auth.verify(current):
            return back("/settings?msg=Aktuelles+Passwort+falsch")
        if new != repeat:
            return back("/settings?msg=Neue+Passw%C3%B6rter+stimmen+nicht+%C3%BCberein")
        try:
            auth.set_password(new)
        except ValueError as exc:
            return back(f"/settings?msg={str(exc).replace(' ', '+')}")
        response = back("/settings?msg=Passwort+ge%C3%A4ndert")
        if os.environ.get("SHOTLAKE_WEB_AUTH", "on").lower() != "off":
            response.set_cookie(  # neue Sitzung; die alte endet mit dem neuen Schlüssel
                COOKIE,
                auth.new_session(),
                max_age=SESSION_DAYS * 86400,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response

    @app.post("/sources/{source}/sync")
    def start_sync(source: str, mode: Annotated[str, Form()] = "incremental") -> Response:
        if source not in API_CONNECTORS or mode not in ("incremental", "full"):
            return Response("Unbekannte Quelle oder Art", status_code=400)
        if not manager.start(source, mode):
            notice[source] = "Es läuft bereits ein Sync."
        return back()

    @app.post("/sources/{source}/check")
    def check_source(source: str) -> Response:
        if source not in API_CONNECTORS:
            return Response("Unbekannte Quelle", status_code=400)
        _ok, text = manager.check(source)
        notice[source] = text
        return back()

    @app.post("/sources/{source}/upload")
    async def upload(source: str, files: Annotated[list[UploadFile], File()]) -> Response:
        if source not in CONNECTORS:
            return Response("Keine Dateiquelle", status_code=400)
        inbox = workspace.data_dir / "inbox" / source
        inbox.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for upload_file in files:
            data = await upload_file.read(MAX_UPLOAD_BYTES + 1)
            if not upload_file.filename:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                lines.append(f"{upload_file.filename}: zu groß (max. 25 MB)")
                continue
            name = SAFE_NAME.sub("_", Path(upload_file.filename).name) or "upload.csv"
            target = inbox / name
            target.write_bytes(data)
            with workspace.session() as con:
                for rep in sync_location(con, source, target, workspace.data_dir / "raw"):
                    lines.append(rep.as_text())
                workspace.run_dedup(con)
        notice[source] = "\n".join(lines) or "Keine Datei ausgewählt."
        return back()

    return manager
