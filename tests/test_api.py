from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from fake_ha import make_settings, wait_until_sync
from homeglass_server.api.deps import require_admin
from homeglass_server.app import create_app
from homeglass_server.ha.auth import HaUser

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "layout.v1.json").read_text(encoding="utf-8"))

API = "http://localhost:8765"
INGRESS = "http://localhost:8099"
AUTH = {"Authorization": "Bearer good-token"}


def test_layout_requires_a_valid_bearer_token(live_settings, fake_ha) -> None:
    with TestClient(create_app(live_settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)

        anonymous = client.get("/api/layout")
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == "Bearer"

        assert client.get("/api/layout", headers={"Authorization": "Bearer faux"}).status_code == 401
        assert client.get("/api/layout", headers={"Authorization": "Basic abc"}).status_code == 401
        assert client.get("/api/ping").status_code == 200


def test_ping_exposes_the_home_assistant_instance_id(live_settings) -> None:
    with TestClient(create_app(live_settings), base_url=API) as client:
        body = client.get("/api/ping").json()

    assert body["ha_uuid"] == "ha-uuid-de-test"
    assert body["schemaVersion"] == 1


def test_layout_is_generated_published_and_cached_with_an_etag(live_settings, fake_ha) -> None:
    with TestClient(create_app(live_settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)

        response = client.get("/api/layout", headers=AUTH)

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        document = response.json()
        jsonschema.validate(document, SCHEMA)
        assert document["revision"] == 1
        assert [room["id"] for room in document["rooms"]] == [
            "room_cuisine",
            "room_salon",
            "room_autres",
        ]

        etag = response.headers["etag"]
        assert etag.startswith('"r1-')
        again = client.get("/api/layout", headers={**AUTH, "If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""

        # ETag faible ou liste : toujours reconnu comme identique.
        weak = client.get("/api/layout", headers={**AUTH, "If-None-Match": f'W/{etag}'})
        assert weak.status_code == 304

        # Un autre ETag renvoie le document complet.
        stale = client.get("/api/layout", headers={**AUTH, "If-None-Match": '"r0-000000000000"'})
        assert stale.status_code == 200


def test_device_registration_reegisters_the_same_device(live_settings, fake_ha) -> None:
    with TestClient(create_app(live_settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)

        first = client.post("/api/devices/register", json={"name": "Pixel"}, headers=AUTH)
        assert first.status_code == 200
        body = first.json()
        assert body["deviceId"].startswith("dev_")
        assert body["revision"] == 1
        assert body["profile"] == {"hiddenRooms": [], "hiddenTabs": [], "themeMode": None}

        second = client.post(
            "/api/devices/register",
            json={"deviceId": body["deviceId"], "model": "Pixel 7"},
            headers=AUTH,
        )
        assert second.json()["deviceId"] == body["deviceId"]

        layout = client.get(
            "/api/layout", params={"device_id": body["deviceId"]}, headers=AUTH
        )
        assert layout.status_code == 200
        assert layout.json()["revision"] == 1


def test_internal_routes_do_not_exist_on_the_api_port(live_settings, fake_ha) -> None:
    with TestClient(create_app(live_settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)

        for path in ("/internal/status", "/internal/entities", "/internal/layout/draft"):
            assert client.get(path, headers=AUTH).status_code == 404

        assert client.post("/internal/layout/publish", headers=AUTH).status_code == 404


def test_ingress_requests_do_not_need_a_token(live_settings, fake_ha) -> None:
    with TestClient(create_app(live_settings), base_url=INGRESS) as client:
        wait_until_sync(fake_ha.ready)

        response = client.get("/api/layout")
        assert response.status_code == 200
        assert client.get("/internal/status").status_code == 200


def test_api_port_is_rate_limited(tmp_path, fake_ha) -> None:
    settings = make_settings(tmp_path, fake_ha, rate_limit_per_minute=2)
    with TestClient(create_app(settings), base_url=API) as client:
        codes = [client.get("/api/ping").status_code for _ in range(3)]

    assert codes == [200, 200, 429]


def test_rate_limit_does_not_apply_to_ingress_port(tmp_path, fake_ha) -> None:
    settings = make_settings(tmp_path, fake_ha, rate_limit_per_minute=1)
    with TestClient(create_app(settings), base_url=INGRESS) as client:
        codes = [client.get("/api/ping").status_code for _ in range(3)]

    assert codes == [200, 200, 200]


def test_require_admin_dependency_returns_403_for_a_standard_user() -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_admin(HaUser(id="u1", is_admin=False)))
    assert error.value.status_code == 403

    admin = asyncio.run(require_admin(HaUser(id="u2", is_admin=True)))
    assert admin.is_admin is True


def test_websocket_rejects_an_invalid_token(live_settings, fake_ha) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(create_app(live_settings), base_url=API) as client:
        wait_until_sync(fake_ha.ready)

        with client.websocket_connect("ws://localhost:8765/api/ws") as socket:
            socket.send_json({"type": "auth", "token": "faux"})
            assert socket.receive_json() == {
                "type": "auth_error",
                "detail": "Jeton invalide ou expiré",
            }
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
