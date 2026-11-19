"""Health-Endpunkte und Schutz der Container-Dateien (Image, Compose, Workflow)."""

import re
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from shotlake.db import open_db
from shotlake.web.app import create_app
from shotlake.workspace import Workspace

REPO = Path(__file__).parent.parent
CLUB_MAP = REPO / "config" / "club_map.toml"
BASELINE = REPO / "baselines" / "hcp40w-approx-v1.toml"


def test_health_endpoints_work_without_login(tmp_path: Path) -> None:
    open_db(tmp_path / "shotlake.duckdb").close()
    client = TestClient(create_app(tmp_path, CLUB_MAP, BASELINE, auth=True))  # Anmeldung an
    assert client.get("/").status_code in (200, 303)  # geschützt (Weiterleitung zu /setup)
    live = client.get("/healthz")
    assert live.status_code == 200 and live.json()["status"] == "ok" and live.json()["version"]
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["db"] == "ok"
    assert "secret" not in live.text.lower() and "password" not in ready.text.lower()


def test_readyz_reports_unavailable_without_leaking_details(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)  # nicht beschreibbar
    try:
        client = TestClient(create_app(data, CLUB_MAP, BASELINE, auth=False))
        r = client.get("/readyz")
        assert r.status_code == 503 and r.json()["status"] == "unavailable"
        assert str(data) not in r.text  # keine Pfade nach außen
    finally:
        data.chmod(0o700)


def test_readyz_stays_ready_while_a_backup_holds_the_lock(tmp_path: Path) -> None:
    open_db(tmp_path / "shotlake.duckdb").close()
    ws = Workspace(tmp_path, CLUB_MAP, BASELINE)
    assert ws._lock.acquire(blocking=False)
    try:
        assert ws.ping() == "busy"  # kein Fehler: eine Sicherung läuft nur kurz
    finally:
        ws._lock.release()
    assert ws.ping() == "ok"


# --- Dateien -------------------------------------------------------------------------------------


def text(name: str) -> str:
    return (REPO / name).read_text()


def test_image_runs_as_non_root_with_one_volume_and_healthcheck() -> None:
    docker = text("Dockerfile")
    assert re.search(r"^USER shotlake", docker, re.M)
    assert 'VOLUME ["/data"]' in docker and "HEALTHCHECK" in docker and "/healthz" in docker
    assert "--no-dev" in docker and "--frozen" in docker  # keine Dev-Pakete, feste Versionen
    assert 'ENTRYPOINT ["entrypoint.sh"]' in docker
    assert not re.search(r"^COPY .*\b(data|secrets|probe-output)\b", docker, re.M)


def test_dockerignore_keeps_data_and_credentials_out_of_the_image() -> None:
    ignored = set(text(".dockerignore").split())
    assert {"data/", "secrets/", "probe-output/", "*.har", ".env", ".git/"} <= ignored


def test_entrypoint_seeds_config_without_overwriting() -> None:
    script = REPO / "docker" / "entrypoint.sh"
    body = script.read_text()
    assert '[ -e "$target" ] ||' in body and body.rstrip().endswith('exec "$@"')
    assert script.stat().st_mode & stat.S_IXUSR


def test_compose_is_local_only_by_default_and_hardened() -> None:
    compose = text("docker-compose.yml")
    assert (
        '"${SHOTLAKE_BIND:-127.0.0.1}:${SHOTLAKE_PORT:-8000}:8000"' in compose
    )  # Standard: nur lokal
    assert "read_only: true" in compose and "cap_drop" in compose and "no-new-privileges" in compose
    assert "shotlake-data:/data" in compose
    assert "ghcr.io/bri-b-dev/shotlake" in compose


def test_workflow_publishes_to_ghcr_with_minimal_permissions() -> None:
    wf = text(".github/workflows/ci.yml")
    assert "ghcr.io/${{ github.repository }}" in wf and "secrets.GITHUB_TOKEN" in wf
    assert "packages: write" in wf
    assert wf.count("packages: write") == 1  # nur im Image-Job, nicht für die Tests
    assert re.search(r"^permissions:\n  contents: read", wf, re.M)  # Standard: nur lesen
    assert "if: github.event_name != 'pull_request'" in wf  # Pull Requests melden sich nicht an
    assert "push: ${{ github.event_name != 'pull_request' }}" in wf
    assert "needs: test" in wf and "linux/amd64,linux/arm64" in wf
    # alle Actions auf eine Hauptversion festgelegt
    assert not re.search(r"uses: [^@\s]+@(main|master|latest)\b", wf)
