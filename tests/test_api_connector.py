"""Contract-Tests für das API-Connector-Interface mit einem Fake-Connector."""

import random
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shotlake.connectors.api import (
    ApiConnector,
    AuthError,
    FetchPage,
    RateLimitedError,
    RawRecord,
    RetryPolicy,
    TransientError,
    load_secret,
    with_retry,
)
from shotlake.connectors.errors import SchemaDriftError
from shotlake.db import open_db
from shotlake.ingest_api import get_cursor, sync_api
from shotlake.model import ParsedSession, ShotRecord

T0 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def doc(n: int, **extra: object) -> RawRecord:
    return RawRecord(f"s{n}", {"id": n, "carry": 100.0 + n, **extra})


class FakeConnector(ApiConnector):
    id = "fake_api"
    required_keys = frozenset({"id", "carry"})
    known_keys = frozenset({"id", "carry"})

    pages: list[list[RawRecord]]
    fail_at: dict[int, Exception]
    cursors: list[str | None]
    auth_calls: int
    sleeps: list[float]

    def authenticate(self) -> None:
        self.auth_calls += 1

    def fetch(self, cursor: str | None) -> Iterator[FetchPage]:
        self.cursors.append(cursor)
        start = int(cursor) if cursor else 0
        for i in range(start, len(self.pages)):
            if i in self.fail_at:
                raise self.fail_at.pop(i)
            yield FetchPage(self.pages[i], next_cursor=str(i + 1), done=i == len(self.pages) - 1)

    def parse_record(self, record: RawRecord) -> ParsedSession:
        carry = record.payload["carry"]
        assert isinstance(carry, float)
        shot = ShotRecord(
            source=self.id,
            shot_key=record.external_id,
            club_raw="7i",
            club_type="iron_7",
            measurement_method="radar",
            carry_m=carry,
        )
        return ParsedSession(self.id, record.external_id, T0, shots=[shot])


def make(pages, fail_at=None) -> FakeConnector:
    c = FakeConnector.__new__(FakeConnector)
    c.sleeps = []
    ApiConnector.__init__(c, sleep=c.sleeps.append)
    c.pages = pages
    c.fail_at = dict(fail_at or {})
    c.cursors = []
    c.auth_calls = 0
    return c


def test_metadata():
    c = make([])
    assert c.mode == "api"
    assert c.risk in {"official", "unofficial_tos_risk"}


def test_sync_stores_raw_canonical_and_cursor(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = make([[doc(1)], [doc(2), doc(3)]])
    rep = sync_api(con, c, tmp_path / "raw")
    assert rep.status == "ok"
    assert rep.sessions_new == 3 and rep.shots_new == 3
    assert get_cursor(con, "fake_api") == "2"
    assert len(list((tmp_path / "raw" / "fake_api").glob("*.json"))) == 3
    assert c.auth_calls == 1


def test_second_sync_is_incremental_and_idempotent(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    pages = [[doc(1)], [doc(2)]]
    sync_api(con, make(pages), None)
    c2 = make([*pages, [doc(3)]])
    rep = sync_api(con, c2, None)
    assert c2.cursors == ["2"]  # setzt am gespeicherten Cursor fort
    assert rep.sessions_new == 1
    # erneuter Lauf mit gleichem Bestand ändert nichts
    rep = sync_api(con, make([*pages, [doc(3)]]), None)
    assert rep.sessions_new == 0 and rep.shots_new == 0


def test_transient_error_retries_from_last_cursor(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = make([[doc(1)], [doc(2)]], fail_at={1: TransientError("503")})
    rep = sync_api(con, c, None)
    assert rep.status == "ok" and rep.sessions_new == 2
    assert c.cursors == [None, "1"]
    assert len(c.sleeps) == 1


def test_persistent_transient_error_gives_up_keeping_cursor(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")

    class Down(FakeConnector):
        def fetch(self, cursor):
            raise TransientError("down")
            yield  # pragma: no cover

    c = Down.__new__(Down)
    c.sleeps = []
    ApiConnector.__init__(c, retry=RetryPolicy(max_attempts=3), sleep=c.sleeps.append)
    c.pages = []
    c.auth_calls = 0
    rep = sync_api(con, c, None)
    assert rep.status == "error"
    assert len(c.sleeps) == 2
    assert get_cursor(con, "fake_api") is None


def test_auth_error_is_not_retried(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    c = make([[doc(1)]])

    def boom() -> None:
        raise AuthError("abgelaufen")

    c.authenticate = boom  # type: ignore[method-assign]
    rep = sync_api(con, c, None)
    assert rep.status == "error" and "Auth" in rep.errors[0]
    assert c.sleeps == [] and c.cursors == []


def test_drift_stops_sync_and_is_reprocessable(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    bad = RawRecord("s2", {"id": 2})  # Pflichtschlüssel `carry` fehlt
    rep = sync_api(con, make([[doc(1)], [bad]]), tmp_path / "raw")
    assert rep.status == "drift_detected"
    assert rep.sessions_new == 1
    assert get_cursor(con, "fake_api") == "1"  # Cursor steht vor der defekten Seite
    assert len(list((tmp_path / "raw" / "fake_api").glob("*.json"))) == 2  # Rohdatei bleibt
    # nach Fix (gleiches Dokument, jetzt gültig) wird es erneut verarbeitet
    rep = sync_api(con, make([[doc(1)], [doc(2)]]), None)
    assert rep.status == "ok" and rep.sessions_new == 1


def test_unknown_key_is_only_a_warning(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, make([[doc(1, newfield="x")]]), None)
    assert rep.status == "ok"
    assert any("newfield" in w for w in rep.warnings)


def test_backoff_policy():
    p = RetryPolicy(base_delay_s=1.0, max_delay_s=10.0, jitter=0.0)
    e = TransientError("x")
    assert [p.delay(a, e) for a in (1, 2, 3, 6)] == [1.0, 2.0, 4.0, 10.0]
    assert p.delay(1, RateLimitedError(7.0)) == 7.0
    assert p.delay(1, RateLimitedError(500.0)) == 10.0
    jittered = RetryPolicy(base_delay_s=4.0, jitter=0.25).delay(1, e, random.Random(1))
    assert 3.0 <= jittered <= 5.0


def test_with_retry_gives_up_and_passes_other_errors():
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("t")
        return "ok"

    assert with_retry(flaky, RetryPolicy(jitter=0.0), sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]

    def bad() -> str:
        raise ValueError("kein Retry")

    with pytest.raises(ValueError):
        with_retry(bad, sleep=sleeps.append)


def test_load_secret_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHOTLAKE_GARMIN_PLATFORM_TOKEN", "abc")
    assert load_secret("garmin-platform", "token") == "abc"
    monkeypatch.delenv("SHOTLAKE_GARMIN_PLATFORM_TOKEN")
    with pytest.raises(AuthError):
        load_secret("garmin-platform", "token")


def test_schema_drift_error_type():
    assert issubclass(SchemaDriftError, Exception)
