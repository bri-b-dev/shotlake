"""Sicherung und Wiederherstellung in der Weboberfläche (Einstellungen).

Download der Sicherung, Schlüssel sichern (mit erneuter Eingabe des Oberflächen-Passworts) und
Wiederherstellen per Datei-Upload. Meldungen enthalten nie Passwörter oder Schlüssel.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from ..backup import (
    BackupError,
    default_archive_name,
    export_key,
    is_encrypted,
    key_source,
    list_safety_backups,
)
from ..workspace import Workspace
from .auth import AuthStore

MAX_UPLOAD_BYTES = 4 * 1024**3
UPLOAD_CHUNK = 1024 * 1024


def backup_context(workspace: Workspace) -> dict[str, Any]:
    """Angaben für den Abschnitt „Sicherung" der Einstellungsseite."""
    safety = [
        {
            "name": p.name,
            "mb": p.stat().st_size / 1024**2,
            "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
        }
        for p in list_safety_backups(workspace.data_dir)
    ]
    return {"key_source": key_source(workspace.data_dir), "safety_backups": safety}


def install_backup(app: FastAPI, workspace: Workspace, auth: AuthStore) -> None:
    def back(message: str) -> RedirectResponse:
        return RedirectResponse(f"/settings?msg={quote(message)}#sicherung", status_code=303)

    @app.post("/settings/backup/download")
    async def download(password: Annotated[str, Form()] = "") -> Response:
        pw = password or None
        folder = workspace.data_dir
        fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".download-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            await run_in_threadpool(workspace.backup, tmp, pw)
        except BackupError as exc:
            tmp.unlink(missing_ok=True)
            return back(str(exc))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return FileResponse(
            tmp,
            filename=default_archive_name(pw is not None),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(tmp.unlink, missing_ok=True),
        )

    @app.get("/settings/backup/file/{name}")
    def safety_file(name: str) -> Response:
        match = next((p for p in list_safety_backups(workspace.data_dir) if p.name == name), None)
        if match is None:
            return Response("Nicht gefunden", status_code=404)
        return FileResponse(
            match,
            filename=name,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/settings/backup/key")
    def key(password: Annotated[str, Form()] = "") -> Response:
        if auth.enabled and not auth.verify(password):
            return back("Passwort der Oberfläche falsch: Schlüssel nicht ausgegeben")
        try:
            value = export_key(workspace.data_dir)
        except BackupError as exc:
            return back(str(exc))
        return Response(
            value + "\n",
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="shotlake-secret.key"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/settings/backup/restore")
    async def restore(
        archive: Annotated[UploadFile, File()],
        password: Annotated[str, Form()] = "",
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        if confirm != "yes":
            return back("Wiederherstellung nicht bestätigt: nichts geändert")
        fd, tmp_name = tempfile.mkstemp(dir=workspace.data_dir, prefix=".upload-")
        tmp = Path(tmp_name)
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                while block := await archive.read(UPLOAD_CHUNK):
                    size += len(block)
                    if size > MAX_UPLOAD_BYTES:
                        return back("Datei zu groß (höchstens 4 GB)")
                    out.write(block)
            if size == 0:
                return back("Keine Datei ausgewählt")
            if is_encrypted(tmp) and not password:
                return back("Das Archiv ist verschlüsselt: Passwort angeben")
            report = await run_in_threadpool(workspace.restore, tmp, password or None)
        except BackupError as exc:
            return back(f"Wiederherstellung fehlgeschlagen, nichts geändert: {exc}")
        finally:
            tmp.unlink(missing_ok=True)
        text = (
            f"Wiederhergestellt: {report.restored['rows']} Zeilen in {report.restored['tables']} "
            f"Tabellen, {report.restored['files']} Dateien."
        )
        if report.safety_backup:
            text += f" Der vorherige Stand liegt als {report.safety_backup.name} unter Sicherungen."
        return back(" ".join([text, *report.notes]))
