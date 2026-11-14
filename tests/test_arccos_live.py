"""Arccos-Live-Client gegen einen Fake-Transport (kein Netz)."""

import base64
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from shotlake.cli import main
from shotlake.connectors.api import AuthError, RateLimitedError, TransientError
from shotlake.connectors.arccos import ArccosConnector
from shotlake.connectors.arccos_live import (
    LiveArccosClient,
    Response,
    UrllibTransport,
    discover_remote_hashes,
)
from shotlake.connectors.errors import SchemaDriftError
from shotlake.db import open_db
from shotlake.devalue import encode_remote_arg
from shotlake.devalue import parse as devalue_parse
from shotlake.ingest_api import sync_api
from tests.test_arccos import detail_of, payload, stringify

BASE = "https://dash.test"
SECRET_VALUES = ("AK-GEHEIM", "SECRET-GEHEIM", "USER-0123")


def decode_arg(payload_b64: str) -> list[Any]:
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))  # type: ignore[no-any-return]


def test_encode_remote_arg_matches_dashboard_format():
    # Form wie im Dashboard-Mitschnitt: sortierte Schlüssel, Werte in derselben Reihenfolge
    got = base64.urlsafe_b64decode(
        encode_remote_arg(
            {"year": 2026, "goalHcp": -20, "includeExcluded": True, "limit": 50,
             "subjectUserId": "", "tournamentOnly": False, "userId": "abc"}
        )  + "=="
    ).decode()  # fmt: skip
    assert got == (
        '[["__skrao",1],{"goalHcp":2,"includeExcluded":3,"limit":4,"subjectUserId":5,'
        '"tournamentOnly":6,"userId":7,"year":8},-20,true,50,"",false,"abc",2026]'
    )
    assert "=" not in encode_remote_arg({"a": 1})
    decoded = devalue_parse(base64.urlsafe_b64decode(encode_remote_arg({"a": 1}) + "==").decode())
    assert decoded["$type"] == "__skrao"


class FakeDashboard:
    """Fake für Startseite, JS-Chunks, Magic-Link und Remote Functions."""

    def __init__(self, details: dict[int, dict[str, Any]], rows_by_year: dict[int, list[Any]]):
        self.details, self.rows_by_year = details, rows_by_year
        self.calls: list[str] = []
        self.login_status = 200
        self.fail_once: Response | None = None
        self.has_more_first = False

    def remote(self, name: str, result: Any) -> Response:
        body = json.dumps({"type": "result", "data": stringify({"_": result, "q": {}})})
        return Response(200, body)

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        self.calls.append(re.sub(r"(accessKey|secret|userId)=[^&]*", r"\1=*", url))
        if "/magic-link?" in url:
            return Response(self.login_status, "", {"x-final-url": BASE + "/"})
        if url == BASE + "/":
            return Response(200, '<script src="/_app/immutable/entry/app.js"></script>')
        if url.endswith("entry/app.js"):
            return Response(200, 'import"../chunks/a.js";import("../nodes/1.js")')
        if url.endswith("chunks/a.js"):
            return Response(200, "t(`ajj89v/roundsBundle`),t(`ajj89v/roundYears`)")
        if url.endswith("nodes/1.js"):
            return Response(200, 'x(`nqbht5/roundDetailBundle`);import"../chunks/b.js"')
        if url.endswith("chunks/b.js"):
            return Response(200, "// nichts Relevantes")
        if "/_app/remote/" in url:
            if self.fail_once:
                resp, self.fail_once = self.fail_once, None
                return resp
            name = url.split("?")[0].rsplit("/", 1)[-1]
            args = decode_arg(url.split("payload=")[1])
            keys = args[1]
            values = {k: args[i] for k, i in keys.items()}
            if name == "roundYears":
                return self.remote(name, sorted(self.rows_by_year, reverse=True))
            if name == "roundsBundle":
                rows = self.rows_by_year[values["year"]]
                more = self.has_more_first and values["limit"] == 50
                return self.remote(
                    name, {"list": {"ok": True, "data": {"rounds": rows, "hasMore": more}}}
                )
            if name == "roundDetailBundle":
                data = self.details[int(values["roundId"])]
                return self.remote(name, {"bundle": {"ok": True, "data": data}})
        return Response(404, "")


def make_dashboard() -> FakeDashboard:
    p1, p2 = payload(11, "2025-07-01T10:00:00.000Z"), payload(12, "2026-07-02T10:00:00.000Z")
    return FakeDashboard(
        {11: detail_of(p1), 12: detail_of(p2)},
        {2026: [p2["summary"]], 2025: [p1["summary"]]},
    )


def client(fake: FakeDashboard) -> LiveArccosClient:
    return LiveArccosClient(
        "USER-0123", "AK-GEHEIM", "SECRET-GEHEIM", transport=fake, base_url=BASE
    )


def test_hash_discovery_walks_imports():
    fake = make_dashboard()
    found = discover_remote_hashes(fake, BASE, {"roundsBundle", "roundDetailBundle"})
    assert found == {"roundsBundle": "ajj89v", "roundDetailBundle": "nqbht5"}
    with pytest.raises(SchemaDriftError, match="nichtDa"):
        discover_remote_hashes(fake, BASE, {"nichtDa"})


def test_live_client_lists_all_years_and_loads_detail():
    fake = make_dashboard()
    c = client(fake)
    c.login()
    rows = c.rounds()
    assert [r["roundId"] for r in rows] == [12, 11]  # beide Jahre, neueste zuerst
    detail = c.round_detail(12)
    assert detail is not None and detail["round"]["meta"]["roundId"] == 12
    assert repr(c) == f"LiveArccosClient(base_url={BASE!r})"  # keine Zugangsdaten im repr


def test_has_more_retries_with_larger_limit():
    fake = make_dashboard()
    fake.has_more_first = True
    c = client(fake)
    c.login()
    c.rounds()
    limits = [decode_arg(u.split("payload=")[1])[4] for u in fake.calls if "roundsBundle" in u]
    assert 50 in limits and 200 in limits


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (401, {}, AuthError),
        (403, {}, AuthError),
        (429, {"retry-after": "7"}, RateLimitedError),
        (503, {}, TransientError),
        (418, {}, SchemaDriftError),
    ],
)
def test_http_errors_map_to_error_classes(status, headers, error):
    fake = make_dashboard()
    c = client(fake)
    c.login()
    fake.fail_once = Response(status, "", headers)
    with pytest.raises(error) as info:
        c.rounds()
    if error is RateLimitedError:
        assert info.value.retry_after_s == 7.0


def test_login_rejected_and_not_logged_in():
    fake = make_dashboard()
    fake.login_status = 401
    with pytest.raises(AuthError):
        client(fake).login()
    with pytest.raises(AuthError, match="Nicht angemeldet"):
        client(make_dashboard()).rounds()


def test_login_redirect_to_login_page_is_auth_error():
    fake = make_dashboard()
    fake.get = lambda url, headers=None: Response(  # type: ignore[method-assign]
        200, "", {"x-final-url": BASE + "/login"}
    )
    with pytest.raises(AuthError):
        client(fake).login()


def test_garbled_response_is_drift_without_leaking_urls():
    fake = make_dashboard()
    c = client(fake)
    c.login()
    fake.fail_once = Response(200, "<html>kaputt</html>")
    with pytest.raises(SchemaDriftError) as info:
        c.rounds()
    assert not any(v in str(info.value) for v in SECRET_VALUES)


def test_sync_through_connector_with_live_client(tmp_path: Path):
    con = open_db(tmp_path / "t.duckdb")
    fake = make_dashboard()
    rep = sync_api(
        con, ArccosConnector(client=client(fake), sleep=lambda _s: None), tmp_path / "raw"
    )
    assert rep.status == "ok" and rep.rounds_new == 2
    text = json.dumps(rep.warnings + rep.errors) + "".join(fake.calls)
    assert "AK-GEHEIM" not in text and "SECRET-GEHEIM" not in text  # Login-Aufruf maskiert


def test_connector_builds_live_client_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("SHOTLAKE_ARCCOS_ACCESS_KEY", raising=False)
    monkeypatch.setenv("SHOTLAKE_SOPS_FILE", str(tmp_path / "gibt-es-nicht.json"))
    con = open_db(tmp_path / "t.duckdb")
    rep = sync_api(con, ArccosConnector(sleep=lambda _s: None), None)
    assert rep.status == "error" and "Zugangsdaten fehlen" in rep.errors[0]
    monkeypatch.setenv("SHOTLAKE_ARCCOS_ACCESS_KEY", "AK-GEHEIM")
    monkeypatch.setenv("SHOTLAKE_ARCCOS_SECRET", "SECRET-GEHEIM")
    monkeypatch.setenv("SHOTLAKE_ARCCOS_USER_ID", "USER-0123")
    c = ArccosConnector(sleep=lambda _s: None)
    c.client = None
    from shotlake.connectors import arccos_live

    monkeypatch.setattr(arccos_live, "UrllibTransport", lambda: make_dashboard())
    monkeypatch.setattr(arccos_live, "BASE_URL", BASE)
    with pytest.raises(AuthError):  # Fake-Dashboard hört auf BASE, Default-URL ist eine andere
        c.authenticate()


def test_cli_arccos_check_reports_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    for var in ("ACCESS_KEY", "SECRET", "USER_ID"):
        monkeypatch.delenv(f"SHOTLAKE_ARCCOS_{var}", raising=False)
    monkeypatch.setenv("SHOTLAKE_SOPS_FILE", str(tmp_path / "nix.json"))
    assert main(["--data-dir", str(tmp_path / "d"), "arccos-check"]) == 2
    assert "Zugangsdaten fehlen" in capsys.readouterr().err


def test_urllib_transport_has_cookie_jar_and_no_url_in_errors():
    t = UrllibTransport(timeout_s=0.01)
    assert t.jar is not None
    url = "http://127.0.0.1:9/magic-link?" + urllib.parse.urlencode({"secret": "SECRET-GEHEIM"})
    with pytest.raises(TransientError) as info:
        t.get(url)
    assert "SECRET-GEHEIM" not in str(info.value)
