"""Strukturierte Logs: Format, Maskierung von Geheimnissen, Ereignisse je Lauf mit Run-ID."""

import json
import logging
from pathlib import Path

import pytest

from shotlake import logs
from shotlake.connectors.api import RateLimitedError, RawRecord
from shotlake.db import open_db
from shotlake.ingest_api import sync_api
from tests.test_api_connector import doc, make


def record(name: str = "x.y", level: int = logging.INFO, **fields) -> logging.LogRecord:
    rec = logging.LogRecord("shotlake.t", level, __file__, 1, name, None, None)
    vars(rec).update(event=name, fields=fields)
    return rec


def test_json_line_has_event_level_and_fields():
    line = json.loads(logs.JsonFormatter().format(record("run.finished", run_id="abc", n=3)))
    assert line["event"] == "run.finished" and line["level"] == "info"
    assert line["run_id"] == "abc" and line["n"] == 3 and line["ts"].endswith("+00:00")


def test_secrets_are_masked_by_key_and_by_pattern():
    out = logs.JsonFormatter().format(
        record(
            password="hunter2",
            api_key="k-123",
            headers={"Authorization": "Bearer abc.def", "Cookie": "sid=1", "ok": "ja"},
            error="Anmeldung mit token=geheim123 und Bearer eyJhbGci.xyz fehlgeschlagen",
            url="https://x.test/magic-link?accessKey=AK123&userId=7",
        )
    )
    for secret in ("hunter2", "k-123", "abc.def", "sid=1", "geheim123", "eyJhbGci", "AK123"):
        assert secret not in out
    assert '"ok": "ja"' in out and out.count("***") >= 5


def test_long_text_is_cut_and_text_format_is_readable():
    assert len(logs.scrub("x" * 5000)) < 2100
    line = logs.TextFormatter().format(
        record("run.finished", source="trackman", status="ok", leer=[])
    )
    assert "run.finished" in line and "source=trackman" in line and "leer" not in line


def test_foreign_loggers_are_formatted_too():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "GET %s", ("/x",), None)
    out = json.loads(logs.JsonFormatter().format(rec))
    assert out["event"] == "uvicorn.access" and out["msg"] == "GET /x"


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_sync_logs_start_and_finish_with_same_run_id(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO, logger="shotlake")
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, make([[doc(1)], [doc(2)]]), None)
    (start,) = events(caplog, "run.started")
    (end,) = events(caplog, "run.finished")
    assert vars(start)["fields"]["run_id"] == vars(end)["fields"]["run_id"] == rep.run_id
    assert vars(end)["fields"]["source"] == "fake_api" and vars(end)["fields"]["status"] == "ok"
    assert vars(end)["fields"]["sessions_new"] == 2 and vars(end)["fields"]["duration_ms"] >= 0
    assert end.levelno == logging.INFO


def test_failed_run_is_logged_as_error_with_cause_and_stored_run_id(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO, logger="shotlake")
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, make([[doc(1)], [RawRecord("s2", {"id": 2})]]), None)
    assert rep.status == "drift_detected"
    (end,) = events(caplog, "run.finished")
    assert end.levelno == logging.ERROR and vars(end)["fields"]["status"] == "drift_detected"
    assert vars(end)["fields"]["error_count"] == 1 and "carry" in vars(end)["fields"]["errors"][0]
    assert con.execute(
        "select count(*) from import_run where run_id = ?", [rep.run_id]
    ).fetchone() == (1,)


def test_retries_are_logged_as_warnings(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO, logger="shotlake")
    con = open_db(tmp_path / "t.duckdb")
    c = make([[doc(1)], [doc(2)]], fail_at={1: RateLimitedError(7.0)})
    rep = sync_api(con, c, None)
    assert rep.status == "ok"
    (retry,) = events(caplog, "request.retry")
    assert retry.levelno == logging.WARNING and vars(retry)["fields"]["attempt"] == 1
    assert vars(retry)["fields"]["error_type"] == "RateLimitedError"


def test_configure_installs_one_handler_and_is_repeatable():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logs.configure("json", "info")
        logs.configure("text", "warning")
        ours = [h for h in root.handlers if h.get_name() == "shotlake"]
        assert len(ours) == 1 and isinstance(ours[0].formatter, logs.TextFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = before
        root.setLevel(logging.WARNING)


def test_health_checks_are_not_logged():
    quiet = logs._QuietHealth()
    health = logging.LogRecord(
        "uvicorn.access", 20, "", 1, '1.2.3.4 - "GET %s HTTP/1.1" 200', ("/healthz",), None
    )
    page = logging.LogRecord(
        "uvicorn.access", 20, "", 1, '1.2.3.4 - "GET %s HTTP/1.1" 200', ("/clubs",), None
    )
    assert not quiet.filter(health) and quiet.filter(page)
