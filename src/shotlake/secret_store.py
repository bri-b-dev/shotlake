"""Verschlüsselter Speicher für Zugangsdaten (Fernet, Datei `secrets.enc` im Datenverzeichnis).

Der Schlüssel kommt aus `SHOTLAKE_SECRET_KEY` (Fernet-Schlüssel, z. B. als Container-Secret),
sonst wird beim ersten Speichern `secret.key` (Rechte 0600) im Datenverzeichnis erzeugt. Liegt der
Schlüssel neben der Datei, schützt das nur vor versehentlichem Mitkopieren der Datei (Backup,
Export), nicht vor Zugriff auf das ganze Verzeichnis; wer mehr will, setzt die Umgebungsvariable.

Werte verlassen den Speicher nur über `get` (für Connectors); `status` nennt nur, welche Felder
gesetzt sind und wann sie zuletzt geändert wurden.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .connectors import api

ENV_KEY = "SHOTLAKE_SECRET_KEY"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = True  # False: darf im Klartext angezeigt werden (z. B. E-Mail)


# Welche Zugangsdaten je Quelle gepflegt werden können (Schlüssel wie in `load_secret`)
CREDENTIAL_FIELDS: dict[str, tuple[Field, ...]] = {
    "bebrassie": (Field("email", "E-Mail", secret=False), Field("password", "Passwort")),
    "arccos": (
        Field("user_id", "Nutzer-ID", secret=False),
        Field("access_key", "Access Key"),
        Field("secret", "Secret"),
    ),
    "trackman": (Field("token", "Bearer-Token"),),
}


class SecretStoreError(Exception):
    """Speicher nicht lesbar (falscher Schlüssel, beschädigte Datei)."""


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SecretStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "secrets.enc"
        self.key_path = data_dir / "secret.key"

    def _fernet(self, create: bool) -> Fernet | None:
        env = os.environ.get(ENV_KEY)
        if env:
            try:
                return Fernet(env.encode())
            except ValueError as exc:
                raise SecretStoreError(f"{ENV_KEY} ist kein gültiger Fernet-Schlüssel") from exc
        if self.key_path.exists():
            return Fernet(self.key_path.read_bytes().strip())
        if not create:
            return None
        key = Fernet.generate_key()
        _write_private(self.key_path, key)
        return Fernet(key)

    def _load(self) -> dict[str, dict[str, dict[str, str]]]:
        if not self.path.exists():
            return {}
        fernet = self._fernet(create=False)
        if fernet is None:
            raise SecretStoreError("Schlüssel für secrets.enc fehlt")
        try:
            data = json.loads(fernet.decrypt(self.path.read_bytes()))
        except (InvalidToken, ValueError) as exc:
            raise SecretStoreError("secrets.enc nicht lesbar (falscher Schlüssel?)") from exc
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, dict[str, str]]]) -> None:
        fernet = self._fernet(create=True)
        assert fernet is not None
        _write_private(self.path, fernet.encrypt(json.dumps(data).encode()))

    def get(self, source: str, key: str) -> str | None:
        entry = self._load().get(source, {}).get(key)
        return entry.get("value") if entry else None

    def set_many(self, source: str, values: dict[str, str]) -> None:
        """Nur nicht leere Werte schreiben; leere Felder lassen bestehende Werte unberührt."""
        data = self._load()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for key, value in values.items():
            if value.strip():
                data.setdefault(source, {})[key] = {"value": value.strip(), "updated": now}
        self._save(data)

    def delete(self, source: str) -> None:
        data = self._load()
        if data.pop(source, None) is not None:
            self._save(data)

    def status(self) -> dict[str, dict[str, str]]:
        """Je Quelle und Feld der Änderungszeitpunkt; nie die Werte."""
        return {
            src: {k: v.get("updated", "") for k, v in fields.items()}
            for src, fields in self._load().items()
        }

    def visible(self, source: str, key: str) -> str | None:
        """Wert eines nicht geheimen Feldes (z. B. E-Mail) zur Anzeige."""
        field = next((f for f in CREDENTIAL_FIELDS.get(source, ()) if f.key == key), None)
        return self.get(source, key) if field and not field.secret else None


def use_store(data_dir: Path) -> SecretStore:
    """Speicher als Quelle für `load_secret` registrieren (nach Umgebung, vor sops)."""
    store = SecretStore(data_dir)

    def provider(source: str, key: str) -> str | None:
        try:
            return store.get(source, key)
        except SecretStoreError:
            return None

    api.register_secret_provider(provider)
    return store
