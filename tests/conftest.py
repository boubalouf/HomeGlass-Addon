from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fake_ha import FakeHaServer, make_settings
from homeglass_server.app import create_app
from homeglass_server.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def example_payload() -> dict:
    return json.loads((FIXTURES / "example_layout.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        web_dir=ROOT / "web",
        schema_dir=ROOT / "schema",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def app(settings: Settings):
    """Application nue pour les tests ASGI bruts (chemins non normalisés par httpx)."""
    return create_app(settings)


@pytest.fixture
def fake_ha():
    """Faux Home Assistant, dans son propre thread."""
    with FakeHaServer() as fake:
        yield fake


@pytest.fixture
def live_settings(tmp_path: Path, fake_ha) -> Settings:
    """Réglages branchés sur le faux Home Assistant (dossier de données temporaire)."""
    return make_settings(tmp_path, fake_ha)
