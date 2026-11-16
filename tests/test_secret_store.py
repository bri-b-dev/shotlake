"""Verschlüsselter Zugangsdaten-Speicher."""

import os
import stat
from pathlib import Path

import pytest

from shotlake.connectors import api
from shotlake.connectors.api import AuthError, load_secret
from shotlake.secret_store import SecretStore, SecretStoreError, use_store


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SHOTLAKE_BEBRASSIE_PASSWORD", raising=False)
    yield
    api._PROVIDERS.clear()


def test_roundtrip_is_encrypted_and_private(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.set_many("bebrassie", {"email": "a@example.org", "password": "pw-GEHEIM"})
    assert store.get("bebrassie", "password") == "pw-GEHEIM"
    raw = store.path.read_bytes()
    assert b"pw-GEHEIM" not in raw and b"a@example.org" not in raw
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.key_path).st_mode) == 0o600


def test_status_never_contains_values(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.set_many("trackman", {"token": "TOK-GEHEIM"})
    status = store.status()
    assert set(status["trackman"]) == {"token"} and "TOK-GEHEIM" not in str(status)
    assert store.visible("trackman", "token") == ""  # Geheimfeld nie sichtbar
    store.set_many("bebrassie", {"email": "a@example.org"})
    assert store.visible("bebrassie", "email") == "a@example.org"


def test_empty_values_keep_existing(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.set_many("bebrassie", {"email": "a@example.org", "password": "alt"})
    store.set_many("bebrassie", {"email": "b@example.org", "password": "  "})
    assert store.get("bebrassie", "email") == "b@example.org"
    assert store.get("bebrassie", "password") == "alt"


def test_delete_and_wrong_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SecretStore(tmp_path)
    store.set_many("trackman", {"token": "t"})
    store.delete("trackman")
    assert store.get("trackman", "token") is None
    store.set_many("trackman", {"token": "t"})
    store.key_path.unlink()
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SHOTLAKE_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(SecretStoreError):
        store.get("trackman", "token")


def test_env_key_is_used_and_no_key_file_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SHOTLAKE_SECRET_KEY", Fernet.generate_key().decode())
    store = SecretStore(tmp_path)
    store.set_many("trackman", {"token": "t"})
    assert not store.key_path.exists() and store.get("trackman", "token") == "t"


def test_load_secret_order_env_then_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_store(tmp_path).set_many("bebrassie", {"password": "aus-speicher"})
    assert load_secret("bebrassie", "password") == "aus-speicher"
    monkeypatch.setenv("SHOTLAKE_BEBRASSIE_PASSWORD", "aus-env")
    assert load_secret("bebrassie", "password") == "aus-env"
    monkeypatch.delenv("SHOTLAKE_BEBRASSIE_PASSWORD")
    with pytest.raises(AuthError):
        load_secret("bebrassie", "email")
