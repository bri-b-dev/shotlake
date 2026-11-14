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


class Workspace:
    """Eine DuckDB-Datei, Verbindung je Aufruf (kurz geöffnet), Zugriffe nacheinander."""

    def __init__(self, data_dir: Path, club_map: Path, baseline_path: Path) -> None:
        self.data_dir = data_dir
        self.club_map = club_map
        self.baseline_path = baseline_path
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
