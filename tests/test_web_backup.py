"""Sicherung und Wiederherstellung in der Oberfläche."""

import io
import tarfile
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from shotlake.backup import create_backup
from shotlake.db import open_db
from shotlake.web.app import create_app
from tests.test_backup import make_data, note_of

REPO = Path(__file__).parent.parent
CLUB_MAP = REPO / "config" / "club_map.toml"
BASELINE = REPO / "baselines" / "hcp18w-approx-v1.toml"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)


def build(data: Path, auth: bool | None = None) -> TestClient:
    app = create_app(data, CLUB_MAP, BASELINE, auth=auth)
    return TestClient(app, follow_redirects=False)


def message(response) -> str:
    assert response.status_code == 303
    return unquote(response.headers["location"])


def test_settings_page_has_backup_section(tmp_path: Path) -> None:
    client = build(make_data(tmp_path / "d"))
    html = client.get("/settings").text
    assert "Sicherung herunterladen" in html and "Schlüssel sichern" in html
    assert "Wiederherstellen" in html and "Nie enthalten" in html


def test_download_is_a_valid_archive_without_key_and_leaves_no_temp_files(tmp_path: Path) -> None:
    data = make_data(tmp_path / "d")
    r = build(data).post("/settings/backup/download", data={"password": ""})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        listed = tar.getnames()
    assert "db/shotlake.duckdb" in listed and "secrets.enc" in listed
    assert not any("secret.key" in n or "auth.json" in n or "garmin" in n for n in listed)
    assert not list(data.glob(".download-*"))


def test_download_with_password_is_encrypted(tmp_path: Path) -> None:
    r = build(make_data(tmp_path / "d")).post(
        "/settings/backup/download", data={"password": "pw-pw-pw-pw"}
    )
    assert r.status_code == 200 and r.content.startswith(b"SHOTLAKE-BACKUP-ENC1")
    assert ".tar.gz.enc" in r.headers["content-disposition"]


def test_backup_is_refused_while_a_sync_runs(tmp_path: Path) -> None:
    from shotlake.backup import BackupError
    from shotlake.workspace import Workspace

    ws = Workspace(make_data(tmp_path / "d"), CLUB_MAP, BASELINE)
    assert ws._sync_lock.acquire(blocking=False)
    try:
        with pytest.raises(BackupError, match="Sync"):
            ws.backup(tmp_path / "x.tar.gz")
        with pytest.raises(BackupError, match="Sync"):
            ws.restore(tmp_path / "x.tar.gz")
    finally:
        ws._sync_lock.release()
    ws.backup(tmp_path / "x.tar.gz")  # danach geht es wieder


def test_key_needs_the_ui_password_when_auth_is_on(tmp_path: Path) -> None:
    data = make_data(tmp_path / "d")
    (data / "auth.json").unlink()  # die Attrappe aus make_data ist kein gültiger Passwort-Hash
    client = build(data, auth=True)
    client.post("/setup", data={"password": "Passwort-fuer-ui-1", "repeat": "Passwort-fuer-ui-1"})
    client.post("/login", data={"password": "Passwort-fuer-ui-1", "next": "/"})
    bad = client.post("/settings/backup/key", data={"password": "falsch"})
    assert "falsch" in message(bad) and (data / "secret.key").read_text().strip() not in bad.text
    good = client.post("/settings/backup/key", data={"password": "Passwort-fuer-ui-1"})
    assert (
        good.status_code == 200 and good.text.strip() == (data / "secret.key").read_text().strip()
    )
    assert good.headers["cache-control"] == "no-store"
    assert "attachment" in good.headers["content-disposition"]


def test_key_from_environment_is_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SHOTLAKE_SECRET_KEY", Fernet.generate_key().decode())
    data = make_data(tmp_path / "d", secrets=False)
    r = build(data).post("/settings/backup/key", data={"password": ""})
    assert "SHOTLAKE_SECRET_KEY" in message(r)
    assert "SHOTLAKE_SECRET_KEY" in build(data).get("/settings").text


def test_restore_via_upload_replaces_data_and_lists_safety_backup(tmp_path: Path) -> None:
    archive = tmp_path / "alt.tar.gz"
    create_backup(make_data(tmp_path / "alt", "alt"), archive)
    live = make_data(tmp_path / "live", "aktuell")
    client = build(live)
    with archive.open("rb") as fh:
        r = client.post(
            "/settings/backup/restore",
            files={"archive": ("alt.tar.gz", fh, "application/gzip")},
            data={"confirm": "yes", "password": ""},
        )
    assert "Wiederhergestellt" in message(r)
    assert note_of(live) == "alt"
    html = client.get("/settings").text
    assert "pre-restore-" in html
    name = next(p.name for p in (live / "backups").iterdir())
    assert client.get(f"/settings/backup/file/{name}").status_code == 200
    assert client.get("/settings/backup/file/..%2Fauth.json").status_code == 404
    assert not list(live.glob(".upload-*"))


def test_restore_needs_confirmation_and_bad_files_change_nothing(tmp_path: Path) -> None:
    live = make_data(tmp_path / "live", "aktuell")
    client = build(live)
    junk = io.BytesIO(b"kein archiv")
    r = client.post(
        "/settings/backup/restore",
        files={"archive": ("x.tar.gz", junk, "application/gzip")},
        data={"confirm": ""},
    )
    assert "nicht bestätigt" in message(r)
    r = client.post(
        "/settings/backup/restore",
        files={"archive": ("x.tar.gz", io.BytesIO(b"kein archiv"), "application/gzip")},
        data={"confirm": "yes"},
    )
    assert "fehlgeschlagen" in message(r) and "nichts geändert" in message(r)
    assert note_of(live) == "aktuell"
    assert not (live / "backups").exists() and not list(live.glob(".upload-*"))


def test_encrypted_restore_needs_the_password(tmp_path: Path) -> None:
    archive = tmp_path / "e.enc"
    create_backup(make_data(tmp_path / "alt", "alt"), archive, password="pw-pw-pw-pw")
    live = make_data(tmp_path / "live", "aktuell")
    client = build(live)

    def post(password: str):
        with archive.open("rb") as fh:
            return client.post(
                "/settings/backup/restore",
                files={"archive": ("e.enc", fh, "application/octet-stream")},
                data={"confirm": "yes", "password": password},
            )

    assert "Passwort angeben" in message(post(""))
    assert "Passwort falsch" in message(post("falsch"))
    assert note_of(live) == "aktuell"
    assert "Wiederhergestellt" in message(post("pw-pw-pw-pw"))
    assert note_of(live) == "alt"


def test_database_is_usable_after_restore(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    create_backup(make_data(tmp_path / "alt", "alt"), archive)
    live = make_data(tmp_path / "live")
    client = build(live)
    with archive.open("rb") as fh:
        client.post(
            "/settings/backup/restore",
            files={"archive": ("a.tar.gz", fh, "application/gzip")},
            data={"confirm": "yes"},
        )
    assert client.get("/").status_code == 200 and client.get("/rounds").status_code == 200
    con = open_db(live / "shotlake.duckdb")
    assert con.execute("select count(*) from round_input").fetchone() == (1,)
    con.close()
