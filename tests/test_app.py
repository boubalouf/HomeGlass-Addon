from __future__ import annotations

from fastapi.testclient import TestClient

from homeglass_server.models import SCHEMA_VERSION
from homeglass_server.settings import Settings


def test_ping_is_public_and_matches_the_contract(client: TestClient) -> None:
    response = client.get("/api/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "homeglass"
    assert body["schemaVersion"] == SCHEMA_VERSION
    assert set(body) == {"app", "version", "ha_uuid", "schemaVersion"}
    assert isinstance(body["version"], str) and body["version"]


def test_ping_does_not_require_authentication(client: TestClient) -> None:
    assert client.get("/api/ping").status_code == 200


def test_ingress_page_is_served(client: TestClient) -> None:
    response = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/token"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "HomeGlass Server" in response.text


def test_openapi_document_is_available(client: TestClient) -> None:
    response = client.get("/api/openapi.json")

    assert response.status_code == 200
    assert "/api/ping" in response.json()["paths"]


def test_options_are_read_from_options_json(tmp_path) -> None:
    options_file = tmp_path / "options.json"
    options_file.write_text(
        '{"log_level": "debug", "api_port": 9000, "allow_remote_access": true}',
        encoding="utf-8",
    )

    settings = Settings.load(options_path=options_file, environ={})

    assert settings.log_level == "debug"
    assert settings.api_port == 9000
    assert settings.allow_remote_access is True


def test_empty_api_port_disables_the_api(tmp_path) -> None:
    options_file = tmp_path / "options.json"
    options_file.write_text('{"api_port": null}', encoding="utf-8")

    settings = Settings.load(options_path=options_file, environ={})

    assert settings.api_port is None
