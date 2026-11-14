from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fake_ha import wait_until_sync
from homeglass_server.app import create_app
from homeglass_server.store.pairing import PairingError, PairingStore

API = "http://localhost:8765"
INGRESS = "http://localhost:8099"


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


# --------------------------------------------------------------------- magasin


def test_request_creates_a_pending_pairing_with_a_six_digit_code(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")

    entry = store.request(device_name="Pixel 7", model="Pixel", address="192.168.1.30")

    assert len(entry["code"]) == 6 and entry["code"].isdigit()
    assert entry["deviceId"].startswith("dev_")

    pending = store.pending()
    assert [item["pairingId"] for item in pending] == [entry["pairingId"]]
    assert pending[0]["code"] == entry["code"]
    assert pending[0]["deviceName"] == "Pixel 7"
    assert pending[0]["expiresIn"] > 0
    assert store.devices() == []


def test_claim_returns_a_token_that_authenticates_once(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")
    entry = store.request(device_name="Pixel 7")

    result = store.claim(entry["pairingId"], entry["code"])

    assert result["token"]
    assert result["deviceId"] == entry["deviceId"]
    assert store.pending() == []

    device = store.authenticate(result["token"])
    assert device is not None and device["deviceId"] == entry["deviceId"]
    assert store.authenticate("jeton-qui-nexiste-pas-0123456789") is None

    # Un code ne sert qu'une fois.
    with pytest.raises(PairingError) as error:
        store.claim(entry["pairingId"], entry["code"])
    assert error.value.status == 404

    # Le jeton reste secret côté disque : seule son empreinte est écrite.
    stored = (tmp_path / "pairing.json").read_text(encoding="utf-8")
    assert result["token"] not in stored


def test_wrong_code_is_rejected_then_the_request_is_cancelled(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json", max_attempts=2)
    entry = store.request()

    with pytest.raises(PairingError) as first:
        store.claim(entry["pairingId"], _wrong(entry["code"]))
    assert first.value.status == 403
    assert len(store.pending()) == 1

    with pytest.raises(PairingError) as second:
        store.claim(entry["pairingId"], _wrong(entry["code"]))
    assert second.value.status == 429
    assert store.pending() == []


def test_expired_pairing_cannot_be_claimed(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entry = PairingStore(tmp_path / "pairing.json", ttl=60, clock=lambda: now).request()

    later = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=61)
    store = PairingStore(tmp_path / "pairing.json", ttl=60, clock=lambda: later)

    assert store.pending() == []
    with pytest.raises(PairingError) as error:
        store.claim(entry["pairingId"], entry["code"])
    assert error.value.status == 404


def test_revoke_invalidates_the_token(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json")
    entry = store.request()
    result = store.claim(entry["pairingId"], entry["code"])

    assert store.count() == 1
    assert store.revoke(entry["deviceId"]) is True
    assert store.authenticate(result["token"]) is None
    assert store.revoke(entry["deviceId"]) is False


def test_pending_requests_are_capped(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "pairing.json", max_pending=2)
    for _ in range(3):
        store.request()

    assert len(store.pending()) == 2


# ------------------------------------------------------------------------- API


def test_pairing_flow_between_the_two_ports(settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url=API) as api, TestClient(app, base_url=INGRESS) as ingress:
        response = api.post(
            "/api/pair/request",
            json={"deviceName": "Pixel 7", "model": "Pixel", "appVersion": "1.0"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["codeLength"] == 6
        assert body["expiresIn"] == 600
        assert "code" not in body
        # Identifiant d'instance : valeur locale persistante en développement, UUID réel en add-on.
        assert body["ha_uuid"]

        overview = ingress.get("/internal/pairing").json()
        assert len(overview["pending"]) == 1
        code = overview["pending"][0]["code"]

        wrong = api.post(
            "/api/pair/claim",
            json={"pairingId": body["pairingId"], "code": _wrong(code)},
        )
        assert wrong.status_code == 403

        claim = api.post(
            "/api/pair/claim", json={"pairingId": body["pairingId"], "code": code}
        )
        assert claim.status_code == 200
        token = claim.json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        assert api.get("/api/layout", headers=headers).status_code == 200
        assert ingress.get("/internal/pairing").json()["devices"][0]["deviceId"] == body["deviceId"]

        assert ingress.delete(f"/internal/pairing/devices/{body['deviceId']}").status_code == 200
        assert api.get("/api/layout", headers=headers).status_code == 401


def test_pairing_codes_are_not_readable_from_the_api_port(settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url=API) as api:
        api.post("/api/pair/request", json={})

        assert api.get("/internal/pairing").status_code == 404
        assert api.delete("/internal/pairing/pending/abc").status_code == 404


def test_claim_with_an_unknown_request_returns_404(settings) -> None:
    with TestClient(create_app(settings), base_url=API) as api:
        response = api.post("/api/pair/claim", json={"pairingId": "inconnu", "code": "123456"})

    assert response.status_code == 404


def test_gateway_requires_authentication(settings) -> None:
    with TestClient(create_app(settings), base_url=API) as api:
        assert api.get("/api/ha/config").status_code == 401
        assert api.post("/api/ha/services/light/toggle", json={}).status_code == 401


def test_gateway_reports_a_missing_supervisor_token(settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url=API) as api, TestClient(app, base_url=INGRESS) as ingress:
        entry = api.post("/api/pair/request", json={}).json()
        pending = ingress.get("/internal/pairing").json()["pending"]
        token = api.post(
            "/api/pair/claim",
            json={"pairingId": entry["pairingId"], "code": pending[0]["code"]},
        ).json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        assert api.get("/api/ha/config", headers=headers).status_code == 503
        assert api.post("/api/ha/services/light/toggle", headers=headers, json={}).status_code == 503
        # Domaine invalide : refusé avant tout appel à Home Assistant.
        assert api.post("/api/ha/services/Light/toggle", headers=headers, json={}).status_code == 422


def test_gateway_websocket_accepts_a_paired_device_and_relays_commands(
    live_settings, fake_ha
) -> None:
    app = create_app(live_settings)
    with TestClient(app, base_url=API) as api, TestClient(app, base_url=INGRESS) as ingress:
        wait_until_sync(fake_ha.ready)

        entry = api.post("/api/pair/request", json={"deviceName": "Pixel"}).json()
        pending = ingress.get("/internal/pairing").json()["pending"]
        token = api.post(
            "/api/pair/claim",
            json={"pairingId": entry["pairingId"], "code": pending[0]["code"]},
        ).json()["token"]

        with api.websocket_connect("ws://localhost:8765/api/ha/ws") as socket:
            assert socket.receive_json()["type"] == "auth_required"
            socket.send_json({"type": "auth", "token": token})
            assert socket.receive_json()["type"] == "auth_ok"

            socket.send_json({"id": 1, "type": "get_states"})
            reply = socket.receive_json()
            assert reply["type"] == "result" and reply["success"] is True
            assert [state["entity_id"] for state in reply["result"]][0] == "light.salon"


def test_gateway_websocket_rejects_an_unknown_token(live_settings, fake_ha) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(create_app(live_settings), base_url=API) as api:
        wait_until_sync(fake_ha.ready)

        with api.websocket_connect("ws://localhost:8765/api/ha/ws") as socket:
            assert socket.receive_json()["type"] == "auth_required"
            socket.send_json({"type": "auth", "token": "faux-jeton"})
            assert socket.receive_json()["type"] == "auth_invalid"
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
