from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def _isolated_club_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests dürfen nie die echte config/club_map.toml lesen oder schreiben."""
    monkeypatch.setenv("SHOTLAKE_CLUB_MAP", str(tmp_path / "club_map.toml"))
