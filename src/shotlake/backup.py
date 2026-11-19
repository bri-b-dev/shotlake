"""Sicherung und Wiederherstellung des Datenverzeichnisses.

Archiv: `tar.gz` mit `manifest.json` als erstem Eintrag, dann
- `db/shotlake.duckdb`: konsistenter Schnappschuss (`COPY FROM DATABASE`),
- `raw/` (Rohdaten, Quelle der Wahrheit für `reprocess`) und `inbox/` (hochgeladene Dateien),
- `secrets.enc` (verschlüsselte Zugangsdaten, nur wenn vorhanden).
Nie im Archiv: `secret.key` (Schlüssel), `auth.json` (Passwort der Oberfläche), `garmin/` (Token),
`cache/` und `backups/`. Ein verlorenes Archiv verrät so keine Zugänge; den Schlüssel sichert man
getrennt (`export_key`).

Optional wird das ganze Archiv mit einem Passwort verschlüsselt (scrypt, Fernet, in 1-MiB-Blöcken).

Wiederherstellung: Manifest und Prüfsummen werden geprüft, bevor etwas ersetzt wird; vorher legt
sie eine Sicherung der aktuellen Daten ab (`backups/`). Sie ersetzt Datenbank, `raw/` und `inbox/`,
fasst aber Schlüssel, Passwort und Tokens nie an. `secrets.enc` wird nur übernommen, wenn der
vorhandene Schlüssel sie lesen kann (sonst landet sie als `secrets.enc.from-backup` daneben).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import struct
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .db import open_db

FORMAT = 1
DB_NAME = "shotlake.duckdb"
ARCHIVE_DB = f"db/{DB_NAME}"
SECRETS_NAME = "secrets.enc"
DATA_DIRS = ("raw", "inbox")
MAGIC = b"SHOTLAKE-BACKUP-ENC1\n"
CHUNK = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024**3  # Obergrenze gegen entpackte Riesen-Archive
MAX_MEMBERS = 200_000
KEEP_SAFETY = 5
REQUIRED_TABLES = ("round", "course_shot", "shot", "session", "raw_object", "import_run")
_MEMBER = re.compile(
    r"manifest\.json|db/shotlake\.duckdb|secrets\.enc|(?:raw|inbox)/[^/\x00\n][^\x00\n]*"
)


class BackupError(Exception):
    """Fehler mit einer für die Anzeige geeigneten Meldung (nie Passwörter oder Schlüssel)."""


@dataclass
class Manifest:
    format: int
    created: str
    encrypted: bool
    duckdb: str
    files: list[dict[str, Any]]
    tables: dict[str, int]
    includes_secrets_file: bool

    def to_json(self) -> bytes:
        return json.dumps(self.__dict__, indent=1, ensure_ascii=False).encode()


@dataclass
class RestoreReport:
    restored: dict[str, int] = field(default_factory=dict)
    safety_backup: Path | None = None
    notes: list[str] = field(default_factory=list)


# --- Verschlüsselung -----------------------------------------------------------------------------


def _fernet(password: str, salt: bytes) -> Fernet:
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(MAGIC)) == MAGIC


def _encrypt_file(source: Path, target: Path, password: str) -> None:
    salt = os.urandom(16)
    fernet = _fernet(password, salt)
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(MAGIC + salt)
        while block := src.read(CHUNK):
            token = fernet.encrypt(block)
            dst.write(struct.pack(">I", len(token)) + token)


def _decrypt_file(source: Path, target: Path, password: str) -> None:
    with source.open("rb") as src, target.open("wb") as dst:
        if src.read(len(MAGIC)) != MAGIC:
            raise BackupError("Kein verschlüsseltes Shotlake-Archiv")
        fernet = _fernet(password, src.read(16))
        while header := src.read(4):
            if len(header) != 4:
                raise BackupError("Archiv beschädigt (abgeschnitten)")
            (size,) = struct.unpack(">I", header)
            token = src.read(size)
            if len(token) != size:
                raise BackupError("Archiv beschädigt (abgeschnitten)")
            try:
                dst.write(fernet.decrypt(token))
            except InvalidToken:
                raise BackupError("Passwort falsch oder Archiv beschädigt") from None


# --- Sichern -------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def _snapshot_db(db: Path, target: Path) -> dict[str, int]:
    """Konsistenter Schnappschuss der Datenbank (Transaktion, ohne Änderung der Quelle)."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{_sql_path(db)}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{_sql_path(target)}' AS snap")
        con.execute("COPY FROM DATABASE src TO snap")
        tables = [
            r[0]
            for r in con.execute(
                "select table_name from duckdb_tables() where database_name = 'src'"
            ).fetchall()
        ]
        counts = {
            t: int(con.execute(f'select count(*) from src."{t}"').fetchone()[0])  # type: ignore[index]
            for t in tables
        }
        con.execute("DETACH snap")
        con.execute("DETACH src")
    except duckdb.Error as exc:
        raise BackupError(
            "Datenbank lässt sich nicht lesen, vermutlich hat sie ein anderer Prozess geöffnet "
            f"(läuft der Server oder ein Sync?): {type(exc).__name__}"
        ) from None
    finally:
        con.close()
    return counts


def _walk(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and not path.name.startswith("."):
            yield path


def create_backup(
    data_dir: Path, target: Path, *, password: str | None = None, now: datetime | None = None
) -> Manifest:
    """Sicherung nach `target` schreiben (atomar: erst temporär, dann umbenennen)."""
    db = data_dir / DB_NAME
    if not db.exists():
        raise BackupError("Keine Datenbank im Datenverzeichnis: nichts zu sichern")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".backup-") as tmp_name:
        tmp = Path(tmp_name)
        snapshot = tmp / "snapshot.duckdb"
        counts = _snapshot_db(db, snapshot)
        sources: list[tuple[str, Path]] = [(ARCHIVE_DB, snapshot)]
        for name in DATA_DIRS:
            base = data_dir / name
            if base.is_dir():
                sources += [(f"{name}/{p.relative_to(base).as_posix()}", p) for p in _walk(base)]
        secrets_file = data_dir / SECRETS_NAME
        if secrets_file.is_file():
            sources.append((SECRETS_NAME, secrets_file))
        files = [
            {"path": name, "size": path.stat().st_size, "sha256": _sha256(path)}
            for name, path in sources
        ]
        manifest = Manifest(
            format=FORMAT,
            created=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
            encrypted=password is not None,
            duckdb=duckdb.__version__,
            files=files,
            tables=counts,
            includes_secrets_file=secrets_file.is_file(),
        )
        plain = tmp / "archive.tar.gz"
        with tarfile.open(plain, "w:gz") as tar:
            raw = manifest.to_json()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime, info.mode = len(raw), int(datetime.now(UTC).timestamp()), 0o600
            tar.addfile(info, io.BytesIO(raw))
            for name, path in sources:
                member = tar.gettarinfo(str(path), arcname=name)
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.mode = 0o600
                with path.open("rb") as fh:
                    tar.addfile(member, fh)
        final = tmp / "final"
        if password is not None:
            _encrypt_file(plain, final, password)
        else:
            plain.replace(final)
        os.chmod(final, 0o600)
        final.replace(target)
    return manifest


# --- Lesen und Prüfen ----------------------------------------------------------------------------


def _open_plain(archive: Path, password: str | None, tmp: Path) -> Path:
    if not archive.is_file():
        raise BackupError("Archiv nicht gefunden")
    if not is_encrypted(archive):
        return archive
    if not password:
        raise BackupError("Das Archiv ist verschlüsselt: Passwort angeben")
    plain = tmp / "plain.tar.gz"
    _decrypt_file(archive, plain, password)
    return plain


def _check_member(member: tarfile.TarInfo) -> None:
    if not member.isreg():
        raise BackupError(f"Unzulässiger Eintrag im Archiv: {member.name!r}")
    name = member.name
    if not _MEMBER.fullmatch(name) or ".." in Path(name).parts or name.startswith("/"):
        raise BackupError(f"Unzulässiger Pfad im Archiv: {name!r}")


def _read_manifest(tar: tarfile.TarFile) -> Manifest:
    first = tar.next()
    if first is None or first.name != "manifest.json":
        raise BackupError("Kein Shotlake-Archiv (manifest.json fehlt am Anfang)")
    handle = tar.extractfile(first)
    try:
        data = json.loads(handle.read(1_000_000)) if handle else {}
        manifest = Manifest(**data)
    except (ValueError, TypeError):
        raise BackupError("manifest.json unlesbar") from None
    if manifest.format != FORMAT:
        raise BackupError(f"Archivformat {manifest.format} wird nicht unterstützt")
    if sum(int(f.get("size", 0)) for f in manifest.files) > MAX_TOTAL_BYTES:
        raise BackupError("Archiv zu groß")
    return manifest


def _extract(tar: tarfile.TarFile, manifest: Manifest, staging: Path) -> None:
    expected = {f["path"]: f for f in manifest.files}
    if len(expected) > MAX_MEMBERS:
        raise BackupError("Archiv enthält zu viele Dateien")
    seen: set[str] = set()
    for member in tar:
        if member.name == "manifest.json":
            continue  # schon gelesen
        _check_member(member)
        entry = expected.get(member.name)
        if entry is None or member.name in seen:
            raise BackupError(f"Eintrag nicht im Manifest: {member.name!r}")
        if member.size != entry["size"]:
            raise BackupError(f"Größe stimmt nicht: {member.name!r}")
        seen.add(member.name)
        target = staging / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tar.extractfile(member)
        assert handle is not None
        digest = hashlib.sha256()
        with target.open("wb") as out:
            while block := handle.read(CHUNK):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != entry["sha256"]:
            raise BackupError(f"Prüfsumme stimmt nicht: {member.name!r}")
    missing = set(expected) - seen
    if missing:
        raise BackupError(f"Dateien fehlen im Archiv: {sorted(missing)[:3]}")


def inspect_backup(archive: Path, password: str | None = None) -> Manifest:
    """Manifest lesen und alle Prüfsummen prüfen, ohne etwas zu ersetzen."""
    with tempfile.TemporaryDirectory(dir=archive.parent, prefix=".inspect-") as tmp_name:
        tmp = Path(tmp_name)
        plain = _open_plain(archive, password, tmp)
        try:
            with tarfile.open(plain, "r:gz") as tar:
                manifest = _read_manifest(tar)
                _extract(tar, manifest, tmp / "staging")
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise BackupError(f"Archiv beschädigt: {type(exc).__name__}") from None
    return manifest


# --- Wiederherstellen ----------------------------------------------------------------------------


def list_safety_backups(data_dir: Path) -> list[Path]:
    folder = data_dir / "backups"
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.name.endswith(".tar.gz")),
        key=lambda p: p.name,
        reverse=True,
    )


def _prune_safety(data_dir: Path) -> None:
    for old in list_safety_backups(data_dir)[KEEP_SAFETY:]:
        old.unlink(missing_ok=True)


def _readable_with_current_key(data_dir: Path, candidate: Path) -> bool | None:
    """True/False: Schlüssel liest die Datei oder nicht; None: es gibt keinen Schlüssel."""
    env = os.environ.get("SHOTLAKE_SECRET_KEY")
    key_path = data_dir / "secret.key"
    try:
        if env:
            fernet = Fernet(env.encode())
        elif key_path.exists():
            fernet = Fernet(key_path.read_bytes().strip())
        else:
            return None
        fernet.decrypt(candidate.read_bytes())
    except (InvalidToken, ValueError):
        return False
    return True


def _replace(staged: Path, live: Path, stamp: str) -> Path | None:
    """`live` durch `staged` ersetzen; liefert den Ort des alten Bestands (oder None)."""
    old: Path | None = None
    if live.exists():
        old = live.with_name(f"{live.name}.replaced-{stamp}")
        live.rename(old)
    try:
        staged.rename(live)
    except OSError:
        if old is not None:
            old.rename(live)
        raise
    return old


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def restore_backup(
    data_dir: Path, archive: Path, *, password: str | None = None, now: datetime | None = None
) -> RestoreReport:
    """Archiv einspielen. Der Aufrufer sorgt dafür, dass nichts auf die Datenbank zugreift."""
    report = RestoreReport()
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data_dir, prefix=".restore-") as tmp_name:
        tmp = Path(tmp_name)
        plain = _open_plain(archive, password, tmp)
        staging = tmp / "staging"
        try:
            with tarfile.open(plain, "r:gz") as tar:
                manifest = _read_manifest(tar)
                _extract(tar, manifest, staging)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise BackupError(f"Archiv beschädigt: {type(exc).__name__}") from None
        staged_db = staging / ARCHIVE_DB
        if not staged_db.is_file():
            raise BackupError("Archiv enthält keine Datenbank")
        try:
            probe = duckdb.connect(str(staged_db), read_only=True)
            try:
                present = {
                    r[0] for r in probe.execute("select table_name from duckdb_tables()").fetchall()
                }
            finally:
                probe.close()
        except duckdb.Error as exc:
            raise BackupError(f"Datenbank im Archiv unbrauchbar: {type(exc).__name__}") from None
        if missing := [t for t in REQUIRED_TABLES if t not in present]:
            raise BackupError(f"Datenbank im Archiv unvollständig: {missing}")
        open_db(staged_db).close()  # wendet Migrationen für ältere Sicherungen an

        if (data_dir / DB_NAME).exists():
            report.safety_backup = data_dir / "backups" / f"pre-restore-{stamp}.tar.gz"
            create_backup(data_dir, report.safety_backup)
            _prune_safety(data_dir)

        done: list[tuple[Path, Path | None]] = []
        try:
            plan = [(staged_db, data_dir / DB_NAME)]
            for name in DATA_DIRS:
                folder = staging / name
                folder.mkdir(exist_ok=True)  # fehlt im Archiv: leerer Bestand statt alter Daten
                plan.append((folder, data_dir / name))
            for staged, live in plan:
                done.append((live, _replace(staged, live, stamp)))
            (data_dir / f"{DB_NAME}.wal").unlink(missing_ok=True)
        except OSError as exc:
            for live, old in reversed(done):
                _remove(live)
                if old is not None:
                    old.rename(live)
            raise BackupError(f"Einspielen fehlgeschlagen und zurückgerollt: {exc}") from None
        for _, old in done:
            if old is not None:
                _remove(old)

        staged_secrets = staging / SECRETS_NAME
        if staged_secrets.exists():
            readable = _readable_with_current_key(data_dir, staged_secrets)
            live_secrets = data_dir / SECRETS_NAME
            if readable is True or not live_secrets.exists():
                shutil.copyfile(staged_secrets, live_secrets)
                os.chmod(live_secrets, 0o600)
                report.notes.append("Zugangsdaten (secrets.enc) übernommen.")
                if readable is None:
                    report.notes.append(
                        "Schlüssel fehlt: `secret.key` zurücklegen oder SHOTLAKE_SECRET_KEY setzen."
                    )
            else:
                side = data_dir / f"{SECRETS_NAME}.from-backup"
                shutil.copyfile(staged_secrets, side)
                os.chmod(side, 0o600)
                report.notes.append(
                    "Der aktuelle Schlüssel kann die gesicherten Zugangsdaten nicht lesen; sie "
                    "liegen "
                    "als secrets.enc.from-backup daneben, die aktuellen blieben unverändert."
                )
        report.restored = {
            "tables": len(manifest.tables),
            "rows": sum(manifest.tables.values()),
            "files": len(manifest.files),
        }
    return report


# --- Schlüssel -----------------------------------------------------------------------------------


def key_source(data_dir: Path) -> str:
    """`env`, `file` oder `none`: woher der Schlüssel für secrets.enc kommt."""
    if os.environ.get("SHOTLAKE_SECRET_KEY"):
        return "env"
    return "file" if (data_dir / "secret.key").exists() else "none"


def export_key(data_dir: Path) -> str:
    """Schlüssel zum getrennten Sichern (Passwortmanager); nur aus der Datei."""
    if key_source(data_dir) == "env":
        raise BackupError("Der Schlüssel kommt aus SHOTLAKE_SECRET_KEY: dort sichern")
    path = data_dir / "secret.key"
    if not path.exists():
        raise BackupError("Es gibt keinen Schlüssel (noch keine Zugangsdaten gespeichert)")
    return path.read_text().strip()


def default_archive_name(encrypted: bool, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"shotlake-backup-{stamp}.tar.gz{'.enc' if encrypted else ''}"
