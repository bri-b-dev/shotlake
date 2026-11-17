"""Arbeitsbereich: Datenverzeichnis, Konfiguration und serialisierter Datenbankzugriff."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from .baseline import Baseline, load_baseline
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

    @contextmanager
    def session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._lock:
            con = open_db(self.data_dir / "shotlake.duckdb")
            try:
                sync_club_map(con, self.club_map)
                yield con
            finally:
                con.close()

    def scope(self, con: duckdb.DuckDBPyConnection) -> Scope:
        """Dedup-Sicht für Auswertungen ohne Quellenfilter."""
        return Scope.build(con, self.dedup_cfg)

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

    def find_baseline(self, baseline_id: str | None) -> Baseline:
        """Baseline nach ID aus dem Verzeichnis der Standard-Baseline; ohne ID die Standard."""
        if baseline_id is None or baseline_id == self.baseline.id:
            return self.baseline
        for path in sorted(self.baseline_path.parent.glob("*.toml")):
            candidate = load_baseline(path)
            if candidate.id == baseline_id:
                return candidate
        known = sorted(p.stem for p in self.baseline_path.parent.glob("*.toml"))
        raise ValueError(f"Unbekannte Baseline {baseline_id!r}; verfügbar: {known}")
