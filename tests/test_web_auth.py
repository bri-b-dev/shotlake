"""Passwortschutz der Weboberfläche."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.web.auth import COOKIE, MAX_FAILURES, AuthStore, _safe_next

REPO = Path(__file__).parent.parent
PW = "ein-langes-passwort"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SHOTLAKE_WEB_AUTH", "on")
    open_db(tmp_path / "shotlake.duckdb").close()
    app = create_app(
        tmp_path, REPO / "config" / "club_map.toml", REPO / "baselines" / "hcp18w-approx-v1.toml"
    )
    return TestClient(app, follow_redirects=False)


def test_first_run_redirects_to_setup_and_sets_password(client: TestClient, tmp_path: Path) -> None:
    r = client.get("/rounds")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert client.post("/setup", data={"password": "kurz", "repeat": "kurz"}).status_code == 400
    assert (
        client.post("/setup", data={"password": PW, "repeat": "anders-anders"}).status_code == 400
    )
    ok = client.post("/setup", data={"password": PW, "repeat": PW})
    assert ok.status_code == 303 and COOKIE in ok.cookies
    assert client.get("/rounds").status_code == 200
    assert PW not in (tmp_path / "auth.json").read_text()  # nur Hash


def test_requires_login_and_wrong_password(client: TestClient) -> None:
    client.post("/setup", data={"password": PW, "repeat": PW})
    client.cookies.clear()
    r = client.get("/clubs")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/clubs")
    assert client.post("/login", data={"password": "falsch-falsch"}).status_code == 401
    ok = client.post("/login", data={"password": PW, "next": "/clubs"})
    assert ok.status_code == 303 and ok.headers["location"] == "/clubs"
    assert client.get("/clubs").status_code == 200


def test_static_stays_open(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200


def test_logout_and_forged_cookie(client: TestClient) -> None:
    client.post("/setup", data={"password": PW, "repeat": PW})
    client.post("/logout")
    client.cookies.clear()
    assert client.get("/").status_code == 303
    client.cookies.set(COOKIE, "AAAA.deadbeef")
    assert client.get("/").status_code == 303


def test_lockout_after_repeated_failures(client: TestClient) -> None:
    client.post("/setup", data={"password": PW, "repeat": PW})
    client.cookies.clear()
    for _ in range(MAX_FAILURES):
        assert client.post("/login", data={"password": "x" * 12}).status_code == 401
    assert client.post("/login", data={"password": PW}).status_code == 429


def test_foreign_origin_post_is_rejected(client: TestClient) -> None:
    client.post("/setup", data={"password": PW, "repeat": PW})
    r = client.post("/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_next_must_be_local() -> None:
    assert _safe_next("/clubs?club=iron_7") == "/clubs?club=iron_7"
    for bad in ("https://evil.example", "//evil.example", "evil", None, "/\\\\evil"):
        assert _safe_next(bad) == "/"


def test_password_change_ends_old_sessions(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    store.set_password(PW)
    cookie = store.new_session()
    assert store.valid_session(cookie)
    store.set_password("ein-anderes-passwort")
    assert not store.valid_session(cookie)
