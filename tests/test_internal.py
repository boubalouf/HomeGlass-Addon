from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from fake_ha import make_settings, wait_until_sync
from homeglass_server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "layout.v1.json").read_text(encoding="utf-8"))
INGRESS = "http://localhost:8099"
API = "http://localhost:8765"
AUTH = {"Authorization": "Bearer good-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def editor(tmp_path, fake_ha):
    """Client Ingress : l'éditeur, sans jeton."""
    settings = make_settings(tmp_path, fake_ha)
    with TestClient(create_app(settings), base_url=INGRESS) as client:
        wait_until_sync(fake_ha.ready)
        yield client


def test_status_describes_the_home_assistant_connection(editor) -> None:
    body = editor.get("/internal/status").json()

    assert body["ha"]["connected"] is True
    assert body["ha"]["version"] == "2026.11.0"
    assert body["ha"]["entities"] == 10
    assert body["ha"]["areas"] == 2
    assert body["ha_uuid"] == "ha-uuid-de-test"
    assert body["publishedRevision"] is None
    assert body["draftExists"] is False
    assert body["apiPort"] == 8765
    assert body["schemaVersion"] == 1


def test_entities_expose_their_exclusion_reason(editor) -> None:
    body = editor.get("/internal/entities").json()
    entities = {item["entityId"]: item for item in body["entities"]}

    assert body["count"] == 10
    assert entities["light.salon"]["excludedReason"] is None
    assert entities["light.salon"]["areaId"] == "salon"
    assert entities["sensor.salon_wifi_signal"]["excludedReason"] == "catégorie diagnostic"
    assert (
        entities["binary_sensor.salon_mouvement"]["excludedReason"]
        == "désactivée dans Home Assistant"
    )
    assert entities["automation.reveil"]["excludedReason"] == "domaine technique"
    assert entities["sensor.salon_temperature"]["unit"] == "°C"


def test_areas_report_their_entity_count(editor) -> None:
    areas = {item["areaId"]: item for item in editor.get("/internal/areas").json()["areas"]}

    assert areas["salon"]["entityCount"] == 5
    assert areas["cuisine"]["entityCount"] == 1


def test_autogenerate_returns_a_valid_layout_without_publishing(editor) -> None:
    response = editor.post("/internal/layout/autogenerate", json={"publish": False})

    assert response.status_code == 200
    document = response.json()
    jsonschema.validate(document, SCHEMA)
    assert document["revision"] == 1
    assert editor.get("/internal/status").json()["publishedRevision"] is None


def test_autogenerate_can_publish_directly(editor) -> None:
    body = editor.post("/internal/layout/autogenerate", json={"publish": True}).json()

    assert body["layout"]["revision"] == 1
    assert body["delivered"] == 0
    assert editor.get("/internal/status").json()["publishedRevision"] == 1


def test_draft_publish_history_and_restore(editor) -> None:
    generated = editor.post("/internal/layout/autogenerate", json={}).json()
    assert editor.put("/internal/layout/draft", json=generated).status_code == 200

    first = editor.post("/internal/layout/publish", json={}).json()
    assert first["layout"]["revision"] == 1

    modified = json.loads(json.dumps(generated))
    modified["home"]["title"] = "Chez moi"
    editor.put("/internal/layout/draft", json={"layout": modified})
    second = editor.post("/internal/layout/publish").json()
    assert second["layout"]["revision"] == 2
    assert second["layout"]["home"]["title"] == "Chez moi"

    history = editor.get("/internal/layout/history").json()
    assert history["limit"] == 20
    assert [version["revision"] for version in history["versions"]] == [2, 1]

    version = editor.get("/internal/layout/versions/1").json()["layout"]
    assert version["home"]["title"] == generated["home"]["title"]

    restored = editor.post("/internal/layout/restore/1").json()
    assert restored["layout"]["revision"] == 3
    assert restored["layout"]["home"]["title"] == generated["home"]["title"]
    assert editor.get("/internal/layout/published").json()["layout"]["revision"] == 3


def test_invalid_draft_is_rejected_with_the_contract_message(editor) -> None:
    response = editor.put(
        "/internal/layout/draft",
        json={"schemaVersion": 1, "publishedAt": "2026-11-04T13:00:00Z", "theme": {"accent": "orange"}},
    )

    assert response.status_code == 422
    assert "mise en page invalide" in response.json()["detail"]
    assert editor.get("/internal/layout/draft").json()["layout"] is None


def test_restore_of_an_unknown_revision_is_a_404(editor) -> None:
    assert editor.post("/internal/layout/restore/99").status_code == 404
    assert editor.get("/internal/layout/versions/99").status_code == 404


def test_publish_notifies_connected_applications(editor) -> None:
    # URL absolue : le port du scope décide du mode (Ingress ou API).
    with editor.websocket_connect("ws://localhost:8099/api/ws") as socket:
        socket.send_json({"type": "auth", "token": "ignoré (Ingress)"})
        hello = socket.receive_json()
        assert hello["type"] == "auth_ok"
        assert hello["user"]["source"] == "ingress"

        generated = editor.post("/internal/layout/autogenerate", json={}).json()
        published = editor.post("/internal/layout/publish", json={"layout": generated}).json()

        assert published["delivered"] == 1
        assert socket.receive_json() == {
            "type": "layout_updated",
            "revision": published["layout"]["revision"],
        }


def test_profiles_are_stored_and_resolved_per_device(editor) -> None:
    generated = editor.post("/internal/layout/autogenerate", json={}).json()
    editor.post("/internal/layout/publish", json={"layout": generated})

    response = editor.put(
        "/internal/devices/dev_tablette/profile",
        json={"hiddenRooms": ["room_cuisine"], "themeMode": "dark"},
    )
    assert response.status_code == 200
    assert response.json()["profile"] == {
        "hiddenRooms": ["room_cuisine"],
        "hiddenTabs": [],
        "themeMode": "dark",
    }

    tablet = editor.get("/api/layout", params={"device_id": "dev_tablette"}).json()
    assert "room_cuisine" not in [room["id"] for room in tablet["rooms"]]
    assert tablet["theme"]["mode"] == "dark"

    other = editor.get("/api/layout").json()
    assert "room_cuisine" in [room["id"] for room in other["rooms"]]
    assert other["theme"]["mode"] == "auto"

    devices = editor.get("/internal/devices").json()
    assert devices["defaultProfile"] == {"hiddenRooms": [], "hiddenTabs": [], "themeMode": None}


def test_asset_upload_and_download(editor, tmp_path, fake_ha) -> None:
    response = editor.post(
        "/internal/assets",
        content=PNG,
        headers={"Content-Type": "image/png", "X-Filename": "fond salon.png"},
    )

    assert response.status_code == 201
    meta = response.json()
    assert meta["contentType"] == "image/png"
    assert meta["id"].startswith("asset_")
    assert editor.get("/internal/assets").json()["count"] == 1

    settings = make_settings(tmp_path, fake_ha)
    with TestClient(create_app(settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)
        download = client.get(f"/api/assets/{meta['id']}", headers=AUTH)
        assert download.status_code == 200
        assert download.headers["content-type"] == "image/png"
        assert download.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert download.content == PNG

        cached = client.get(
            f"/api/assets/{meta['id']}",
            headers={**AUTH, "If-None-Match": download.headers["etag"]},
        )
        assert cached.status_code == 304
        assert client.get("/api/assets/asset_0000000000000000", headers=AUTH).status_code == 404


def test_invalid_asset_upload_is_refused(editor) -> None:
    response = editor.post(
        "/internal/assets",
        content=b"ceci n'est pas une image",
        headers={"Content-Type": "image/png"},
    )

    assert response.status_code == 400
    assert "format non pris en charge" in response.json()["detail"]


def test_oversized_asset_upload_is_refused(tmp_path, fake_ha) -> None:
    settings = make_settings(tmp_path, fake_ha, max_asset_bytes=1024)
    with TestClient(create_app(settings), base_url=INGRESS) as client:
        wait_until_sync(fake_ha.ready)
        response = client.post(
            "/internal/assets",
            content=PNG + b"0" * 2000,
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 413
