"""Arbeitsbereich: Datenverzeichnis, Konfiguration und serialisierter Datenbankzugriff."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from .baseline import Baseline, load_baseline
from .config import sync_club_map
from .db import open_db
from .dedup import (
    DedupConfig,
    RunReport,
    Scope,
    default_dedup_config_path,
    load_config,
)
from .dedup import run as run_dedup
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
