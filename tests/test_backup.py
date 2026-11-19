"""Sicherung und Wiederherstellung: Inhalt, Verschlüsselung, Prüfungen, Rückrollen."""

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from shotlake import backup
from shotlake.backup import (
    BackupError,
    create_backup,
    export_key,
    inspect_backup,
    is_encrypted,
    list_safety_backups,
    restore_backup,
)
from shotlake.db import open_db
from shotlake.secret_store import SecretStore

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOTLAKE_SECRET_KEY", raising=False)


def make_data(root: Path, marker: str = "eins", secrets: bool = True) -> Path:
    """Datenverzeichnis mit Datenbank (Nutzereingabe), Rohdaten, Upload, Zugangsdaten und Tokens."""
    root.mkdir(parents=True, exist_ok=True)
    con = open_db(root / "shotlake.duckdb")
    con.execute(
        "insert into round_input (round_id, handicap_relevant, note, updated_at) "
        "values ('t|r1', true, ?, now()::timestamp)",
        [marker],
    )
    con.close()
    (root / "raw" / "trackman").mkdir(parents=True)
    (root / "raw" / "trackman" / "a.json").write_text(f'{{"marker": "{marker}"}}')
    (root / "inbox" / "mevo").mkdir(parents=True)
    (root / "inbox" / "mevo" / "up.csv").write_text("a,b\n1,2\n")
    (root / "garmin").mkdir()
    (root / "garmin" / "garmin_tokens.json").write_text('{"token": "GARMIN-GEHEIM"}')
    (root / "auth.json").write_text('{"hash": "AUTH-GEHEIM"}')
    (root / "cache").mkdir()
    (root / "cache" / "x").write_text("cache")
    if secrets:
        SecretStore(root).set_many("trackman", {"token": "TOKEN-GEHEIM"})
    return root


def note_of(root: Path) -> str | None:
    con = open_db(root / "shotlake.duckdb")
    try:
        row = con.execute("select note from round_input where round_id = 't|r1'").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def names(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


# --- Inhalt --------------------------------------------------------------------------------------


def test_backup_contains_data_and_secrets_file_but_never_key_auth_or_tokens(tmp_path: Path) -> None:
    data = make_data(tmp_path / "data")
    archive = tmp_path / "b.tar.gz"
    manifest = create_backup(data, archive, now=NOW)
    listed = names(archive)
    assert listed[0] == "manifest.json"
    assert {"db/shotlake.duckdb", "raw/trackman/a.json", "inbox/mevo/up.csv", "secrets.enc"} <= set(
        listed
    )
    assert not any(x in " ".join(listed) for x in ("secret.key", "auth.json", "garmin", "cache"))
    assert manifest.includes_secrets_file and manifest.tables["round_input"] == 1
    blob = archive.read_bytes()
    assert (
        b"TOKEN-GEHEIM" not in blob and b"AUTH-GEHEIM" not in blob and b"GARMIN-GEHEIM" not in blob
    )


def test_backup_is_a_consistent_snapshot_and_leaves_the_source_untouched(tmp_path: Path) -> None:
    data = make_data(tmp_path / "data")
    before = (data / "shotlake.duckdb").stat().st_mtime_ns
    create_backup(data, tmp_path / "b.tar.gz")
    assert note_of(data) == "eins"
    assert (data / "shotlake.duckdb").stat().st_mtime_ns >= before


def test_backup_without_database_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="Keine Datenbank"):
        create_backup(tmp_path, tmp_path / "b.tar.gz")


# --- Wiederherstellen ----------------------------------------------------------------------------


def test_restore_into_fresh_directory(tmp_path: Path) -> None:
    src = make_data(tmp_path / "src")
    archive = tmp_path / "b.tar.gz"
    create_backup(src, archive)
    target = tmp_path / "neu"
    report = restore_backup(target, archive)
    assert note_of(target) == "eins"
    assert (target / "raw" / "trackman" / "a.json").read_text() == '{"marker": "eins"}'
    assert (target / "inbox" / "mevo" / "up.csv").exists()
    assert (target / "secrets.enc").exists()
    assert report.safety_backup is None and report.restored["files"] >= 4
    assert any("Schlüssel fehlt" in n for n in report.notes)  # Schlüssel kommt nie aus dem Archiv
    assert not (target / "secret.key").exists() and not (target / "auth.json").exists()
    assert not list(target.glob(".restore-*"))


def test_restore_replaces_data_keeps_credentials_and_leaves_safety_backup(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "alt", "alt"), archive)
    live = make_data(tmp_path / "live", "neuer-stand", secrets=False)
    report = restore_backup(live, archive, now=NOW)
    assert note_of(live) == "alt"
    assert (live / "raw" / "trackman" / "a.json").read_text() == '{"marker": "alt"}'
    assert (live / "garmin" / "garmin_tokens.json").exists()  # Token und Passwort bleiben
    assert (live / "auth.json").read_text() == '{"hash": "AUTH-GEHEIM"}'
    safety = report.safety_backup
    assert safety is not None and safety.exists() and safety in list_safety_backups(live)
    assert not list(live.glob("*.replaced-*"))
    # die Sicherung vom Vorzustand lässt sich wieder einspielen
    again = tmp_path / "zurueck"
    restore_backup(again, safety)
    assert note_of(again) == "neuer-stand"


def test_safety_backups_are_pruned(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "alt"), archive)
    live = make_data(tmp_path / "live")
    for minute in range(backup.KEEP_SAFETY + 3):
        restore_backup(live, archive, now=datetime(2026, 11, 19, 12, minute, tzinfo=UTC))
    assert len(list_safety_backups(live)) == backup.KEEP_SAFETY


def test_secrets_with_other_key_are_not_swapped_in(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "alt"), archive)
    live = make_data(tmp_path / "live", "x")  # eigener, anderer Schlüssel
    before = (live / "secrets.enc").read_bytes()
    report = restore_backup(live, archive)
    assert (live / "secrets.enc").read_bytes() == before
    assert (live / "secrets.enc.from-backup").exists()
    assert any("kann die gesicherten Zugangsdaten nicht lesen" in n for n in report.notes)
    assert SecretStore(live).get("trackman", "token") == "TOKEN-GEHEIM"  # aktuelle bleiben nutzbar


def test_secrets_with_same_key_are_restored(tmp_path: Path) -> None:
    src = make_data(tmp_path / "src")
    archive = tmp_path / "b.tar.gz"
    create_backup(src, archive)
    live = make_data(tmp_path / "live", "x", secrets=False)
    (live / "secret.key").write_bytes(
        (src / "secret.key").read_bytes()
    )  # Schlüssel getrennt gesichert
    SecretStore(live).set_many("arccos", {"user_id": "u"})  # anderer Stand
    restore_backup(live, archive)
    assert SecretStore(live).get("trackman", "token") == "TOKEN-GEHEIM"


# --- Verschlüsselung -----------------------------------------------------------------------------


def test_encrypted_archive_needs_the_password(tmp_path: Path) -> None:
    data = make_data(tmp_path / "data")
    archive = tmp_path / "b.tar.gz.enc"
    manifest = create_backup(data, archive, password="geheim-passwort")
    assert manifest.encrypted and is_encrypted(archive)
    blob = archive.read_bytes()
    assert b"marker" not in blob and b"round_input" not in blob and b"manifest" not in blob
    with pytest.raises(BackupError, match="Passwort angeben"):
        restore_backup(tmp_path / "x", archive)
    with pytest.raises(BackupError, match="Passwort falsch"):
        restore_backup(tmp_path / "x", archive, password="falsch")
    assert not (tmp_path / "x" / "shotlake.duckdb").exists()
    restore_backup(tmp_path / "ok", archive, password="geheim-passwort")
    assert note_of(tmp_path / "ok") == "eins"


def test_truncated_encrypted_archive_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "b.enc"
    create_backup(make_data(tmp_path / "data"), archive, password="pw-pw-pw-pw")
    archive.write_bytes(archive.read_bytes()[:-50])
    with pytest.raises(BackupError, match="beschädigt"):
        inspect_backup(archive, "pw-pw-pw-pw")


# --- Prüfungen gegen manipulierte Archive --------------------------------------------------------


def crafted(
    path: Path, members: dict[str, bytes], files: list[dict[str, Any]] | None = None
) -> Path:
    """Archiv von Hand bauen; `files` ist das Manifest (Standard: passend zu den Mitgliedern)."""
    entries = files
    if entries is None:
        entries = [
            {"path": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for n, b in members.items()
        ]
    manifest = {
        "format": 1, "created": "x", "encrypted": False, "duckdb": "x", "files": entries,
        "tables": {}, "includes_secrets_file": False,
    }  # fmt: skip
    with tarfile.open(path, "w:gz") as tar:
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for name, body in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    return path


def test_path_traversal_is_rejected_before_anything_is_written(tmp_path: Path) -> None:
    evil = crafted(tmp_path / "evil.tar.gz", {"raw/../../evil.txt": b"x"})
    target = tmp_path / "data"
    with pytest.raises(BackupError, match="Unzulässiger Pfad"):
        restore_backup(target, evil)
    assert not (tmp_path / "evil.txt").exists() and not (target / "shotlake.duckdb").exists()


def test_symlink_member_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "link.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        raw = json.dumps({
            "format": 1, "created": "x", "encrypted": False, "duckdb": "x",
            "files": [], "tables": {}, "includes_secrets_file": False,
        }).encode()  # fmt: skip
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        link = tarfile.TarInfo("raw/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(BackupError, match="Unzulässiger Eintrag"):
        restore_backup(tmp_path / "data", path)


def test_member_not_in_manifest_and_wrong_checksum_are_rejected(tmp_path: Path) -> None:
    unlisted = crafted(tmp_path / "a.tar.gz", {"raw/x.json": b"{}"}, files=[])
    with pytest.raises(BackupError, match="nicht im Manifest"):
        inspect_backup(unlisted)
    wrong = crafted(
        tmp_path / "b.tar.gz",
        {"raw/x.json": b"{}"},
        files=[{"path": "raw/x.json", "size": 2, "sha256": "0" * 64}],
    )
    with pytest.raises(BackupError, match="Prüfsumme"):
        inspect_backup(wrong)


def test_archive_without_database_is_rejected(tmp_path: Path) -> None:
    only_raw = crafted(tmp_path / "c.tar.gz", {"raw/x.json": b"{}"})
    with pytest.raises(BackupError, match="keine Datenbank"):
        restore_backup(tmp_path / "data", only_raw)


def test_foreign_file_is_not_a_backup(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"das ist kein archiv")
    with pytest.raises(BackupError, match="beschädigt"):
        inspect_backup(junk)


def test_inspect_reports_manifest_of_valid_archive(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "data"), archive, now=NOW)
    manifest = inspect_backup(archive)
    assert manifest.created.startswith("2026-11-19T12:00") and manifest.format == 1


# --- Rückrollen ----------------------------------------------------------------------------------


def test_failure_while_swapping_rolls_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "alt", "alt"), archive)
    live = make_data(tmp_path / "live", "bleibt")
    original = backup._replace
    calls = {"n": 0}

    def flaky(staged: Path, target: Path, stamp: str) -> Path | None:
        calls["n"] += 1
        if calls["n"] == 3:  # Datenbank und raw sind schon getauscht, inbox schlägt fehl
            raise OSError("Platte voll")
        return original(staged, target, stamp)

    monkeypatch.setattr(backup, "_replace", flaky)
    with pytest.raises(BackupError, match="zurückgerollt"):
        restore_backup(live, archive)
    assert note_of(live) == "bleibt"
    assert (live / "raw" / "trackman" / "a.json").read_text() == '{"marker": "bleibt"}'
    assert (live / "inbox" / "mevo" / "up.csv").exists()


# --- Schlüssel -----------------------------------------------------------------------------------


def test_export_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BackupError, match="keinen Schlüssel"):
        export_key(tmp_path)
    data = make_data(tmp_path / "data")
    assert export_key(data) == (data / "secret.key").read_text().strip()
    monkeypatch.setenv("SHOTLAKE_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(BackupError, match="SHOTLAKE_SECRET_KEY"):
        export_key(data)
    assert backup.key_source(data) == "env"


def test_analyses_are_identical_after_restore_on_empty_instance(tmp_path: Path) -> None:
    """Spec: nach dem Einspielen auf leerer Instanz liefern Analysen dieselben Ergebnisse."""
    from shotlake.analytics.distance import club_stats
    from shotlake.analytics.score import round_scores
    from shotlake.connectors.trackman import TrackmanConnector
    from shotlake.ingest_api import sync_api
    from tests.test_api_fixtures import TrackmanFakeClient

    data = tmp_path / "data"
    con = open_db(data / "shotlake.duckdb")
    sync_api(con, TrackmanConnector(client=TrackmanFakeClient()), data / "raw")
    before_scores = [
        (r.round_id, r.gross, r.differential, r.simulated)
        for r in round_scores(con, source="trackman")
    ]
    before_clubs = [
        (c.club_type, c.source, c.method, c.n, c.carry_median_m) for c in club_stats(con, min_n=1)
    ]
    con.close()
    assert before_scores and before_clubs

    archive = tmp_path / "b.tar.gz"
    create_backup(data, archive)
    fresh = tmp_path / "fresh"
    restore_backup(fresh, archive)
    con = open_db(fresh / "shotlake.duckdb")
    after_scores = [
        (r.round_id, r.gross, r.differential, r.simulated)
        for r in round_scores(con, source="trackman")
    ]
    after_clubs = [
        (c.club_type, c.source, c.method, c.n, c.carry_median_m) for c in club_stats(con, min_n=1)
    ]
    con.close()
    assert after_scores == before_scores and after_clubs == before_clubs
    assert (fresh / "raw" / "trackman").is_dir() and any((fresh / "raw" / "trackman").iterdir())


def test_snapshot_uses_the_portable_storage_format(tmp_path: Path) -> None:
    """Der Schnappschuss hat die Dateiversion von DuckDB 1.0 (64), nicht die der laufenden Version.

    DuckDB 2.x schreibt sonst ein Format (Version 999), das DuckDB 1.x nicht öffnen kann.
    """
    import struct
    import tarfile

    archive = tmp_path / "b.tar.gz"
    create_backup(make_data(tmp_path / "data"), archive)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile("db/shotlake.duckdb")
        assert member is not None
        header = member.read(32)
    assert header[8:12] == b"DUCK"
    assert struct.unpack("<Q", header[12:20])[0] == 64
