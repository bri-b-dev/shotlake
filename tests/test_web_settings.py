"""Zugangsdaten, Sync-Knopf, Prüfen und Upload in der Weboberfläche."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotlake.connectors.api import AuthError
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.sync import parse_arccos_link
from tests.test_api_connector import doc, make

REPO = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "pw-GEHEIM-123"


def build(tmp_path: Path, factory: Any = None) -> TestClient:
    open_db(tmp_path / "shotlake.duckdb").close()
    app = create_app(
        tmp_path,
        REPO / "config" / "club_map.toml",
        REPO / "baselines" / "hcp18w-approx-v1.toml",
        connector_factory=factory,
    )
    return TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)
    for var in ("EMAIL", "PASSWORD"):
        monkeypatch.delenv(f"SHOTLAKE_BEBRASSIE_{var}", raising=False)


def test_credentials_saved_encrypted_and_never_shown(tmp_path: Path) -> None:
    client = build(tmp_path)
    r = client.post(
        "/settings/credentials/bebrassie", data={"email": "a@example.org", "password": SECRET}
    )
    assert r.status_code == 303
    page = client.get("/settings").text
    assert "a@example.org" in page  # E-Mail darf stehen
    assert SECRET not in page and "gesetzt" in page
    assert SECRET.encode() not in (tmp_path / "secrets.enc").read_bytes()
    # leeres Passwortfeld lässt das Passwort unverändert
    client.post("/settings/credentials/bebrassie", data={"email": "b@example.org", "password": ""})
    from shotlake.secret_store import SecretStore

    assert SecretStore(tmp_path).get("bebrassie", "password") == SECRET


def test_delete_and_unknown_source(tmp_path: Path) -> None:
    client = build(tmp_path)
    client.post("/settings/credentials/trackman", data={"token": "T"})
    client.post("/settings/credentials/trackman/delete")
    from shotlake.secret_store import SecretStore

    assert SecretStore(tmp_path).get("trackman", "token") is None
    assert client.post("/settings/credentials/nix", data={}).status_code == 404


def test_arccos_magic_link_is_parsed() -> None:
    url = "https://dashboard.arccosgolf.com/magic-link?accessKey=AK&secret=SE&userId=42"
    assert parse_arccos_link(url) == {"access_key": "AK", "secret": "SE", "user_id": "42"}
    assert parse_arccos_link("kein link") == {}


def test_arccos_link_fills_three_fields(tmp_path: Path) -> None:
    client = build(tmp_path)
    url = "https://x.test/magic-link?accessKey=AKVALUE9&secret=SE&userId=42"
    client.post("/settings/credentials/arccos", data={"magic_link": url})
    from shotlake.secret_store import SecretStore

    store = SecretStore(tmp_path)
    assert (store.get("arccos", "access_key"), store.get("arccos", "user_id")) == ("AKVALUE9", "42")
    assert "AKVALUE9" not in client.get("/settings").text  # Geheimfeld bleibt verdeckt


def test_stored_credentials_reach_the_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = build(tmp_path)
    client.post("/settings/credentials/trackman", data={"token": "TOK-123"})
    from shotlake.connectors.api import load_secret

    assert load_secret("trackman", "token") == "TOK-123"


def wait_done(client: TestClient, timeout: float = 10.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        html = client.get("/sources").text
        if "läuft …" not in html:
            return html
        time.sleep(0.1)
    raise AssertionError("Sync wurde nicht fertig")


def test_sync_button_runs_in_background_and_reports(tmp_path: Path) -> None:
    fake = make([[doc(1)], [doc(2)]])
    client = build(tmp_path, factory=lambda _s: fake)
    assert (
        client.post("/sources/garmin_platform/sync", data={"mode": "incremental"}).status_code
        == 303
    )
    html = wait_done(client)
    assert "fertig" in html and "Sessions neu: 2" in html
    assert fake.cursors == [None]


def test_second_sync_refused_while_running(tmp_path: Path) -> None:
    gate = threading.Event()
    fake = make([[doc(1)]])
    original = fake.fetch

    def slow(cursor: str | None):
        gate.wait(5)
        yield from original(cursor)

    fake.fetch = slow  # type: ignore[method-assign]
    client = build(tmp_path, factory=lambda _s: fake)
    client.post("/sources/garmin_platform/sync", data={"mode": "incremental"})
    client.post("/sources/arccos/sync", data={"mode": "incremental"})
    html = client.get("/sources").text
    assert "läuft …" in html and "Es läuft bereits ein Sync." in html
    assert "disabled" in html
    gate.set()
    wait_done(client)


def test_check_reports_auth_failure_without_secret(tmp_path: Path) -> None:
    class Bad:
        def authenticate(self) -> None:
            raise AuthError(f"abgelehnt {SECRET}"[:12])

    client = build(tmp_path, factory=lambda _s: Bad())
    client.post("/sources/bebrassie/check")
    html = client.get("/sources").text
    assert "Anmeldung abgelehnt" in html and SECRET not in html


def test_upload_imports_csv_and_rejects_non_file_sources(tmp_path: Path) -> None:
    client = build(tmp_path)
    csv = next((FIXTURES / "mevo").glob("*.csv"))
    r = client.post(
        "/sources/mevo/upload", files=[("files", (csv.name, csv.read_bytes(), "text/csv"))]
    )
    assert r.status_code == 303
    html = client.get("/sources").text
    assert "Import mevo" in html and "ok" in html
    assert (tmp_path / "inbox" / "mevo").exists()
    assert (
        client.post(
            "/sources/arccos/upload", files=[("files", ("a.csv", b"x", "text/csv"))]
        ).status_code
        == 400
    )
    evil = client.post(
        "/sources/mevo/upload", files=[("files", ("../../evil.csv", b"a,b\n", "text/csv"))]
    )
    assert evil.status_code == 303
    assert not (tmp_path.parent / "evil.csv").exists()


def test_password_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOTLAKE_WEB_AUTH", "on")
    client = build(tmp_path)
    pw = "ein-langes-passwort"
    client.post("/setup", data={"password": pw, "repeat": pw})
    bad = client.post(
        "/settings/password", data={"current": "falsch", "new": "x" * 12, "repeat": "x" * 12}
    )
    assert "falsch" in bad.headers["location"]
    new = "ein-neues-passwort"
    ok = client.post("/settings/password", data={"current": pw, "new": new, "repeat": new})
    assert "ge%C3%A4ndert" in ok.headers["location"]
    assert client.get("/settings").status_code == 200  # neue Sitzung gesetzt
    client.cookies.clear()
    assert client.post("/login", data={"password": new}).status_code == 303
