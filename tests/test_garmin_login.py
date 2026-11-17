"""Garmin-Erstanmeldung mit Zwei-Faktor-Code (Bibliothek ersetzt durch Attrappen)."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotlake.connectors.garmin_login import GarminLogin, GarminLoginError
from shotlake.connectors.garmin_platform import GarminPlatformConnector
from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.workspace import Workspace

REPO = Path(__file__).parent.parent
CLUB_MAP = REPO / "config" / "club_map.toml"
BASELINE = REPO / "baselines" / "hcp18w-approx-v1.toml"
PW = "garmin-GEHEIM-1"


class AuthenticationError(Exception):
    pass


class FakeGarmin:
    def __init__(self, mfa: bool) -> None:
        self.mfa = mfa
        self.client = self

    def login(self) -> tuple[str | None, Any]:
        return ("needs_mfa", {"s": 1}) if self.mfa else (None, None)

    def resume_login(self, state: Any, code: str) -> None:
        if code != "123456" or state != {"s": 1}:
            raise AuthenticationError("bad code")

    def dump(self, path: str) -> None:
        (Path(path) / "garmin_tokens.json").write_text("{}")


def factory(mfa: bool = True) -> Any:
    def make(email: str | None, password: str | None, want_mfa: bool) -> FakeGarmin:
        if email is not None and password != PW:
            raise AuthenticationError(f"bad password {password}")
        return FakeGarmin(mfa)

    return make


def test_login_with_mfa_then_code(tmp_path: Path) -> None:
    login = GarminLogin(tmp_path / "g", factory())
    assert login.start("a@example.org", PW) == "mfa"
    assert login.pending() and not login.tokens_present()
    with pytest.raises(GarminLoginError):
        login.finish("000000")
    assert login.pending()  # erneuter Versuch möglich
    login.finish("123456")
    assert login.tokens_present() and not login.pending()


def test_login_without_mfa_saves_token(tmp_path: Path) -> None:
    login = GarminLogin(tmp_path / "g", factory(mfa=False))
    assert login.start("a@example.org", PW) == "ok"
    assert login.tokens_present()


def test_wrong_password_message_has_no_credentials(tmp_path: Path) -> None:
    login = GarminLogin(tmp_path / "g", factory())
    with pytest.raises(GarminLoginError) as err:
        login.start("a@example.org", "falsch-XYZ")
    assert "falsch-XYZ" not in str(err.value) and err.value.__cause__ is None


def test_pending_expires(tmp_path: Path) -> None:
    login = GarminLogin(tmp_path / "g", factory(), ttl=-1)
    login.start("a@example.org", PW)
    assert not login.pending()
    with pytest.raises(GarminLoginError):
        login.finish("123456")


def test_web_flow(tmp_path: Path) -> None:
    open_db(tmp_path / "shotlake.duckdb").close()
    app = create_app(tmp_path, CLUB_MAP, BASELINE, garmin_factory=factory())
    client = TestClient(app, follow_redirects=False)
    r = client.post("/settings/garmin/login", data={"email": "a@example.org", "password": PW})
    assert r.status_code == 303
    page = client.get("/settings").text
    assert 'name="code"' in page and PW not in page
    client.post("/settings/garmin/code", data={"code": "123456"})
    page = client.get("/settings").text
    assert "Token vorhanden" in page and 'name="code"' not in page
    assert (tmp_path / "garmin" / "garmin_tokens.json").exists()
    connector = Workspace(tmp_path, CLUB_MAP, BASELINE).make_connector("garmin_platform")
    assert isinstance(connector, GarminPlatformConnector)
    assert connector.tokenstore == str(tmp_path / "garmin")
