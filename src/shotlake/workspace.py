"""Arbeitsbereich: Datenverzeichnis, Konfiguration und serialisierter Datenbankzugriff."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from .backup import BackupError, Manifest, RestoreReport, create_backup, restore_backup
from .baseline import Baseline, list_baselines, load_baseline
from .config import sync_club_map
from .connectors.api import ApiConnector
from .connectors.arccos import ArccosConnector
from .connectors.garmin_platform import GarminPlatformConnector
from .connectors.registry import API_CONNECTORS
from .db import open_db
from .dedup import (
    DedupConfig,
    RunReport,
    Scope,
    default_dedup_config_path,
    load_config,
)
from .dedup import run as run_dedup
from .export import ExportResult, export_zip
from .ingest import ImportReport
from .ingest_api import sync_api
from .secret_store import use_store


class Workspace:
    """Eine DuckDB-Datei, Verbindung je Aufruf (kurz geöffnet), Zugriffe nacheinander."""

    def __init__(
        self,
        data_dir: Path,
        club_map: Path,
        baseline_path: Path,
        dedup_config: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.secrets = use_store(data_dir)
        self.club_map = club_map
        self.baseline_path = baseline_path
        self.dedup_config = (
            dedup_config if dedup_config is not None else default_dedup_config_path()
        )
        self.dedup_cfg: DedupConfig = load_config(self.dedup_config)
        self.baseline: Baseline = load_baseline(baseline_path)
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._club_sync_key: tuple[int, ...] | None = None
        self._schema_ready = False

    @contextmanager
    def session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._lock:
            con = open_db(self.data_dir / "shotlake.duckdb", ensure_schema=not self._schema_ready)
            self._schema_ready = (
                True  # Schema und Migration je Prozess einmal (spart Zeit je Seite)
            )
            try:
                self._sync_club_map(con)
                yield con
            finally:
                con.close()

    def _sync_club_map(self, con: duckdb.DuckDBPyConnection) -> None:
        """Zuordnung nur angleichen, wenn sich Dateien oder Schlagzahl geändert haben."""
        files = (self.club_map, self.club_map.with_name("club_overrides.toml"))
        stamps = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in files)
        row = con.execute("select count(*) from shot").fetchone()
        key = (*stamps, int(row[0]) if row else 0)
        if key == self._club_sync_key:
            return
        sync_club_map(con, self.club_map)
        self._club_sync_key = key

    def ping(self) -> str:
        """Health-Check: `ok` oder `busy` (Sicherung läuft, kein Fehler); sonst eine Ausnahme."""
        if not self._lock.acquire(blocking=False):
            return "busy"
        try:
            con = open_db(self.data_dir / "shotlake.duckdb")
            try:
                con.execute("select 1").fetchone()
            finally:
                con.close()
        finally:
            self._lock.release()
        return "ok"

    def scope(self, con: duckdb.DuckDBPyConnection) -> Scope:
        """Dedup-Sicht für Auswertungen ohne Quellenfilter."""
        return Scope.build(con, self.dedup_cfg)

    def backup(self, target: Path, password: str | None = None) -> Manifest:
        """Sicherung schreiben; hält Seiten und Syncs an, damit kein Schreiber dazwischenfunkt."""
        if not self._sync_lock.acquire(blocking=False):
            raise BackupError("Es läuft gerade ein Sync: später erneut versuchen")
        try:
            with self._lock:
                return create_backup(self.data_dir, target, password=password)
        finally:
            self._sync_lock.release()

    def export(self, target: Path) -> ExportResult:
        """Gesamt-Export als ZIP; hält Seiten und Syncs an (einheitlicher Stand)."""
        if not self._sync_lock.acquire(blocking=False):
            raise BackupError("Es läuft gerade ein Sync: später erneut versuchen")
        try:
            with self._lock:
                return export_zip(self.data_dir / "shotlake.duckdb", target)
        finally:
            self._sync_lock.release()

    def restore(self, archive: Path, password: str | None = None) -> RestoreReport:
        """Sicherung einspielen (ersetzt Datenbank, Rohdaten und Uploads)."""
        if not self._sync_lock.acquire(blocking=False):
            raise BackupError("Es läuft gerade ein Sync: später erneut versuchen")
        try:
            with self._lock:
                self._schema_ready = False  # die Datenbank wird ersetzt
                self._club_sync_key = None
                return restore_backup(self.data_dir, archive, password=password)
        finally:
            self._sync_lock.release()

    @property
    def garmin_dir(self) -> Path:
        return self.data_dir / "garmin"

    def make_connector(self, source: str) -> ApiConnector:
        connector = API_CONNECTORS[source]()
        if isinstance(connector, ArccosConnector):
            connector.cache_dir = self.data_dir / "cache"
        if (
            isinstance(connector, GarminPlatformConnector)
            and (self.garmin_dir / "garmin_tokens.json").exists()
        ):
            connector.tokenstore = str(self.garmin_dir)  # in der Oberfläche angemeldet
        return connector

    def run_sync(
        self, make: Callable[[], ApiConnector], mode: str = "incremental"
    ) -> ImportReport | None:
        """Sync mit eigener Verbindung (blockiert die Seiten nicht); None: es läuft schon einer.

        `full` ignoriert den gespeicherten Cursor. Danach Dedup, damit neue Runden sofort mit
        anderen Quellen abgeglichen sind.
        """
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            connector = make()  # erst unter der Sperre: Zugangsdaten werden erst jetzt gelesen
            con = open_db(self.data_dir / "shotlake.duckdb")
            try:
                sync_club_map(con, self.club_map)
                if mode == "full":
                    con.execute("delete from sync_state where source = ?", [connector.id])
                rep = sync_api(con, connector, self.data_dir / "raw")
                run_dedup(con, self.dedup_cfg)
                return rep
            finally:
                con.close()
        finally:
            self._sync_lock.release()

    def run_dedup(self, con: duckdb.DuckDBPyConnection) -> RunReport:
        return run_dedup(con, self.dedup_cfg)

    def available_baselines(self) -> list[Baseline]:
        """Alle Baselines im Verzeichnis der Standard-Baseline (Standard zuerst)."""
        found = list_baselines(self.baseline_path.parent)
        by_id = {b.id: b for b in found if b.id != self.baseline.id}
        return [self.baseline, *by_id.values()]

    def find_baseline(self, baseline_id: str | None) -> Baseline:
        """Baseline nach ID; ohne ID die Standard-Baseline."""
        if baseline_id is None or baseline_id == self.baseline.id:
            return self.baseline
        for candidate in self.available_baselines():
            if candidate.id == baseline_id:
                return candidate
        known = sorted(b.id for b in self.available_baselines())
        raise ValueError(f"Unbekannte Baseline {baseline_id!r}; verfügbar: {known}")
