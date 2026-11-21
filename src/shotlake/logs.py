"""Strukturierte Logs: eine Zeile je Ereignis (JSON oder Text), Geheimnisse maskiert.

Ereignisse tragen einen Namen (`run.finished`, `sync.crashed`, ...) und Felder (Run-ID, Quelle,
Dauer, Zähler, Fehler). Ausgabe nur im Server (`configure`; Umgebung
`SHOTLAKE_LOG_FORMAT=json|text` und `SHOTLAKE_LOG_LEVEL`); die Kommandozeilenbefehle
bleiben still, sie zeigen ihre Berichte selbst.
Zugangsdaten gehören nicht in Logs: Felder mit Namen wie `password` oder `token` werden durch
`***` ersetzt, Texte von bekannten Mustern (`Bearer …`, `token=…`) bereinigt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

ROOT = "shotlake"
SENSITIVE_KEY = re.compile(
    r"pass(word|wort)?|secret|token|authorization|cookie|api[_-]?key|access[_-]?key", re.I
)
_SCRUBBERS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (
        re.compile(
            r"(?i)\b(password|passwort|passwd|secret|token|accesskey|access_key|api[_-]?key)"
            r"(\s*[=:]\s*|\"\s*:\s*\")([^\s&\",;]+)"
        ),
        r"\1\2***",
    ),
)
MAX_TEXT = 2000

# Ohne Konfiguration (Tests, Kommandozeile) nichts ausgeben, auch keine Warnungen auf stderr.
logging.getLogger(ROOT).addHandler(logging.NullHandler())


def scrub(text: str) -> str:
    """Bekannte Muster von Zugangsdaten in Freitext maskieren und lange Texte kürzen."""
    for pattern, repl in _SCRUBBERS:
        text = pattern.sub(repl, text)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def clean(value: Any, key: str = "") -> Any:
    """Wert für die Ausgabe vorbereiten: sensible Felder maskieren, Texte bereinigen."""
    if key and SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {str(k): clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [clean(v) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return scrub(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": getattr(record, "event", None) or record.name,
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(clean(fields))
        elif not hasattr(record, "event"):
            entry["msg"] = scrub(record.getMessage())  # Fremd-Logger (uvicorn, ...)
        if record.exc_info:
            entry["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        event = getattr(record, "event", None)
        if event is None:
            line = f"{ts} {record.levelname.lower():7s} {scrub(record.getMessage())}"
        else:
            fields = clean(getattr(record, "fields", {}) or {})
            pairs = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, "", [], {}))
            line = f"{ts} {record.levelname.lower():7s} {event} {pairs}".rstrip()
        if record.exc_info:
            line += "\n" + scrub(self.formatException(record.exc_info))
        return line


def event(logger: str, level: int, name: str, exc_info: bool = False, **fields: Any) -> None:
    """Ereignis `name` mit Feldern protokollieren (Logger `shotlake.<logger>`)."""
    logging.getLogger(f"{ROOT}.{logger}").log(
        level, name, extra={"event": name, "fields": fields}, exc_info=exc_info
    )


class _QuietHealth(logging.Filter):
    """Zugriffe der Gesundheitsprüfung (Docker, alle 30 s) nicht protokollieren."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not any(f" {path} HTTP" in text for path in ("/healthz", "/readyz"))


def configure(fmt: str | None = None, level: str | None = None) -> None:
    """Ausgabe auf stderr einrichten (für den Server); mehrfacher Aufruf ersetzt die Einrichtung."""
    fmt = (fmt or os.environ.get("SHOTLAKE_LOG_FORMAT") or "text").lower()
    level_name = (level or os.environ.get("SHOTLAKE_LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.set_name("shotlake")
    handler.addFilter(_QuietHealth())
    root = logging.getLogger()
    for h in [h for h in root.handlers if h.get_name() == "shotlake"]:
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # eigene Handler ersetzen
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
