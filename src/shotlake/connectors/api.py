"""Interface für API-Connectors: Auth, inkrementelles `fetch(cursor)`, Retry mit Backoff.

Ein API-Connector liefert Rohdokumente (`RawRecord`, JSON). Diese werden unverändert im raw-Layer
abgelegt (Hash-Register wie bei Dateien) und danach per `parse_record` ins Canonical-Modell
überführt. Der Cursor ist ein opaker String, den der Connector selbst definiert (z. B. letzte
Rundenzeit); er wird erst nach erfolgreich verarbeiteter Seite gespeichert.

Fehlerklassen: `AuthError` (nicht wiederholbar), `TransientError`/`RateLimitedError` (Retry mit
exponentiellem Backoff, `Retry-After` hat Vorrang), `SchemaDriftError` (Antwortform geändert).
Zugangsdaten kommen aus Umgebungsvariablen oder einer sops-Datei, nie aus dem Repo.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from ..model import ParsedRound, ParsedSession
from .base import ContractReport


class AuthError(Exception):
    """Anmeldung fehlgeschlagen oder Sitzung abgelaufen; kein automatischer Retry."""


class TransientError(Exception):
    """Vorübergehender Fehler (Netz, 5xx); Wiederholung sinnvoll."""


class RateLimitedError(TransientError):
    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__(f"Rate-Limit (Retry-After={retry_after_s})")
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class RawRecord:
    """Ein unverändertes Rohdokument der Quelle."""

    external_id: str
    payload: dict[str, object]
    kind: str = "session"  # session | round | activity ...

    def to_bytes(self) -> bytes:
        return json.dumps(self.payload, sort_keys=True, ensure_ascii=False).encode()


@dataclass(frozen=True)
class FetchPage:
    records: list[RawRecord]
    next_cursor: str | None  # None: Quelle erschöpft, Cursor unverändert lassen
    done: bool = True


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    jitter: float = 0.25

    def delay(self, attempt: int, exc: TransientError, rng: random.Random | None = None) -> float:
        if isinstance(exc, RateLimitedError) and exc.retry_after_s is not None:
            return float(min(exc.retry_after_s, self.max_delay_s))
        raw = min(self.base_delay_s * float(2 ** (attempt - 1)), self.max_delay_s)
        spread = raw * self.jitter
        jitter: float = (rng or random).uniform(-spread, spread)
        return raw + jitter


def with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """`fn` wiederholen, solange `TransientError` auftritt; sonst Fehler sofort weiterreichen."""
    attempt = 1
    while True:
        try:
            return fn()
        except TransientError as exc:
            if attempt >= policy.max_attempts:
                raise
            sleep(policy.delay(attempt, exc, rng))
            attempt += 1


_PROVIDERS: list[Callable[[str, str], str | None]] = []


def register_secret_provider(provider: Callable[[str, str], str | None]) -> None:
    """Zusätzliche Quelle für Zugangsdaten (z. B. der verschlüsselte Speicher); ersetzt keine
    Umgebungsvariable. Es gilt immer nur der zuletzt registrierte Anbieter."""
    _PROVIDERS[:] = [provider]


def load_secret(source: str, key: str, sops_file: Path | None = None) -> str:
    """Zugangsdaten holen: Umgebung `SHOTLAKE_<SOURCE>_<KEY>`, dann Speicher, dann sops-Datei."""
    env_name = f"SHOTLAKE_{source}_{key}".upper().replace("-", "_")
    if env_name in os.environ:
        return os.environ[env_name]
    for provider in _PROVIDERS:
        value = provider(source, key)
        if value:
            return value
    if sops_file is not None and sops_file.exists():
        out = subprocess.run(
            ["sops", "--decrypt", "--output-type", "json", str(sops_file)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        value = json.loads(out).get(source, {}).get(key)
        if isinstance(value, str):
            return value
    raise AuthError(f"Zugangsdaten fehlen: {env_name} (oder sops-Eintrag {source}.{key})")


@dataclass
class ApiConnector(ABC):
    """Basis für API-Quellen. `sleep` ist injizierbar, damit Tests nicht warten."""

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    notes: list[str] = field(default_factory=list, repr=False)  # Hinweise für den Sync-Report

    id: ClassVar[str]
    mode: ClassVar[Literal["api"]] = "api"
    risk: ClassVar[Literal["official", "unofficial_tos_risk"]] = "unofficial_tos_risk"
    description: ClassVar[str] = ""
    required_keys: ClassVar[frozenset[str]] = frozenset()
    known_keys: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    def authenticate(self) -> None:
        """Sitzung herstellen/erneuern; `AuthError` bei ungültigen Zugangsdaten."""

    @abstractmethod
    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        """Seiten ab `cursor` liefern (None: von Anfang). Transiente Fehler wirft die Quelle."""

    def stable_payload(self, record: RawRecord) -> dict[str, object]:
        """Dokument ohne flüchtige Felder (z. B. Änderungszeitstempel) für den raw-Hash."""
        return record.payload

    @abstractmethod
    def parse_record(self, record: RawRecord) -> ParsedSession | ParsedRound:
        """Rohdokument ins Canonical-Modell; `SchemaDriftError` bei geänderter Form."""

    def contract_check(self, record: RawRecord) -> ContractReport:
        """Pflichtschlüssel fehlen = Fehler, neue Schlüssel = Warnung (Top-Level)."""
        keys = set(record.payload)
        errors: tuple[str, ...] = ()
        missing = sorted(self.required_keys - keys)
        if missing:
            errors = (f"Pflichtschlüssel fehlen: {missing}",)
        unknown = sorted(keys - self.known_keys) if self.known_keys else []
        warnings = (f"Unbekannte Schlüssel: {unknown}",) if unknown else ()
        return ContractReport(errors=errors, warnings=warnings)
