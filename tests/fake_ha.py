"""Faux Home Assistant : serveur WebSocket minimal pour les tests.

Deux points d'entrée, comme dans la réalité :

* ``/core/websocket`` : proxy du Supervisor, authentifié par ``SUPERVISOR_TOKEN`` ;
  implémente ``get_states``, les trois registres, ``get_config``, ``subscribe_events`` ;
* ``/api/websocket`` : API de Core, authentifiée par le jeton de l'utilisateur ;
  implémente ``auth/current_user``.

Le serveur tourne dans son propre thread et son propre boucle d'événements : les tests
peuvent ainsi piloter l'application FastAPI (TestClient, synchrone) sans bloquer le faux
Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.server import serve

from homeglass_server.ha.cache import HaCache
from homeglass_server.settings import Settings

HA_VERSION = "2026.11.0"

AREAS = [
    {"area_id": "salon", "name": "Salon", "icon": "mdi:sofa", "floor_id": None},
    {"area_id": "cuisine", "name": "Cuisine", "icon": None, "floor_id": None},
]

DEVICES = [
    {
        "id": "dev_lampe",
        "name": "Lampe salon",
        "area_id": "salon",
        "manufacturer": "IKEA",
        "model": "TRÅDFRI",
    },
    {
        "id": "dev_plafond",
        "name": "Plafonnier cuisine",
        "area_id": "cuisine",
        "manufacturer": "Legrand",
        "model": "Niloé",
    },
]

ENTITIES = [
    {
        "entity_id": "light.salon",
        "original_name": "Lampe salon",
        "name": None,
        "area_id": None,
        "device_id": "dev_lampe",
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "zha",
    },
    {
        "entity_id": "light.cuisine_plafond",
        "original_name": "Plafonnier",
        "name": None,
        "area_id": "cuisine",
        "device_id": "dev_plafond",
        "icon": "mdi:ceiling-light",
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "zha",
    },
    {
        "entity_id": "sensor.salon_temperature",
        "original_name": "Température",
        "name": None,
        "area_id": "salon",
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "mqtt",
    },
    {
        "entity_id": "sensor.salon_wifi_signal",
        "original_name": "Signal Wi-Fi",
        "name": None,
        "area_id": "salon",
        "device_id": None,
        "icon": None,
        "entity_category": "diagnostic",
        "disabled_by": None,
        "hidden_by": None,
        "platform": "zha",
    },
    {
        "entity_id": "binary_sensor.salon_mouvement",
        "original_name": "Mouvement",
        "name": None,
        "area_id": "salon",
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": "user",
        "hidden_by": None,
        "platform": "zha",
    },
    {
        "entity_id": "lock.entree",
        "original_name": "Serrure entrée",
        "name": None,
        "area_id": "salon",
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "mqtt",
    },
    {
        "entity_id": "switch.bureau",
        "original_name": "Prise bureau",
        "name": None,
        "area_id": None,
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "esphome",
    },
    {
        "entity_id": "scene.soiree",
        "original_name": "Soirée",
        "name": None,
        "area_id": None,
        "device_id": None,
        "icon": "mdi:sofa-single",
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "homeassistant",
    },
    {
        "entity_id": "scene.reveil",
        "original_name": "Réveil",
        "name": None,
        "area_id": None,
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "homeassistant",
    },
    {
        "entity_id": "automation.reveil",
        "original_name": "Réveil automatique",
        "name": None,
        "area_id": None,
        "device_id": None,
        "icon": None,
        "entity_category": None,
        "disabled_by": None,
        "hidden_by": None,
        "platform": "automation",
    },
]

STATES = [
    {
        "entity_id": "light.salon",
        "state": "on",
        "attributes": {
            "friendly_name": "Lampe salon",
            "brightness": 180,
            "supported_color_modes": ["brightness"],
            "supported_features": 1,
        },
    },
    {
        "entity_id": "light.cuisine_plafond",
        "state": "off",
        "attributes": {"friendly_name": "Plafonnier", "brightness": None},
    },
    {
        "entity_id": "sensor.salon_temperature",
        "state": "20.5",
        "attributes": {
            "friendly_name": "Température",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
        },
    },
    {
        "entity_id": "sensor.salon_wifi_signal",
        "state": "-52",
        "attributes": {"friendly_name": "Signal Wi-Fi", "unit_of_measurement": "dBm"},
    },
    {
        "entity_id": "binary_sensor.salon_mouvement",
        "state": "off",
        "attributes": {"friendly_name": "Mouvement", "device_class": "motion"},
    },
    {
        "entity_id": "lock.entree",
        "state": "locked",
        "attributes": {"friendly_name": "Serrure entrée"},
    },
    {
        "entity_id": "switch.bureau",
        "state": "off",
        "attributes": {"friendly_name": "Prise bureau"},
    },
    {
        "entity_id": "scene.soiree",
        "state": "scening",
        "attributes": {"friendly_name": "Soirée", "icon": "mdi:sofa-single"},
    },
    {
        "entity_id": "scene.reveil",
        "state": "scening",
        "attributes": {"friendly_name": "Réveil"},
    },
    {
        "entity_id": "automation.reveil",
        "state": "on",
        "attributes": {"friendly_name": "Réveil automatique"},
    },
]

USERS: dict[str, dict[str, Any]] = {
    "good-token": {
        "id": "1111111111114f4da1d0d0c0b0a09080",
        "name": "Timofei",
        "is_admin": True,
        "is_owner": True,
    },
    "user-token": {
        "id": "2222222222224f4da1d0d0c0b0a09080",
        "name": "Invité",
        "is_admin": False,
        "is_owner": False,
    },
}


class FakeHomeAssistant:
    """Serveur WebSocket de Home Assistant, pilotable depuis les tests."""

    supervisor_token = "supervisor-token"

    def __init__(self) -> None:
        self.areas = [dict(area) for area in AREAS]
        self.devices = [dict(device) for device in DEVICES]
        self.entities = [dict(entity) for entity in ENTITIES]
        self.states = [dict(state) for state in STATES]
        self.users = dict(USERS)

        self.commands: list[dict[str, Any]] = []
        self.subscribers: list[tuple[Any, int, str]] = []
        self.connections: list[Any] = []
        self.authenticated: dict[Any, dict[str, Any]] = {}
        self.supervisor_connections = 0
        self.auth_connections = 0

        self._server: Any | None = None
        self.port: int | None = None

    # ------------------------------------------------------------------ cycle de vie

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for websocket in list(self.connections):
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 - fermeture best effort
                pass
        self.connections.clear()
        self.subscribers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self.port = None

    async def drop_connections(self) -> None:
        """Coupe brutalement toutes les connexions (simule un redémarrage de Core)."""
        for websocket in list(self.connections):
            try:
                await websocket.close(code=1011)
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------------- URL

    @property
    def base_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    @property
    def supervisor_url(self) -> str:
        return f"{self.base_url}/core/websocket"

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/api/websocket"

    # ------------------------------------------------------------------- requêtes

    def command_types(self) -> list[str]:
        return [command.get("type") for command in self.commands]

    def subscribed_events(self) -> set[str]:
        return {event for _, _, event in self.subscribers}

    def ready(self) -> bool:
        """Vrai quand le cache de l'application a fini de s'abonner."""
        return {
            "state_changed",
            "area_registry_updated",
            "device_registry_updated",
            "entity_registry_updated",
        } <= self.subscribed_events()

    async def fire(self, event_type: str, data: dict[str, Any]) -> None:
        """Diffuse un événement à tous les abonnés du type demandé."""
        for websocket, message_id, subscribed in list(self.subscribers):
            if subscribed != event_type:
                continue
            await websocket.send(
                json.dumps(
                    {
                        "id": message_id,
                        "type": "event",
                        "event": {"event_type": event_type, "data": data, "origin": "LOCAL"},
                    }
                )
            )

    # ------------------------------------------------------------------- protocole

    async def _handle(self, websocket: Any) -> None:
        path = websocket.request.path
        self.connections.append(websocket)
        try:
            await websocket.send(json.dumps({"type": "auth_required", "ha_version": HA_VERSION}))
            message = json.loads(await websocket.recv())
            if message.get("type") != "auth":
                await websocket.close(code=1002)
                return

            if path.startswith("/api/websocket"):
                self.auth_connections += 1
                user = self.users.get(message.get("access_token"))
            else:
                self.supervisor_connections += 1
                user = (
                    {"id": "supervisor", "name": "Supervisor", "is_admin": True, "is_owner": True}
                    if message.get("access_token") == self.supervisor_token
                    else None
                )
            if user is None:
                await websocket.send(
                    json.dumps({"type": "auth_invalid", "message": "Invalid access token"})
                )
                return

            self.authenticated[websocket] = user
            await websocket.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))

            async for raw in websocket:
                command = json.loads(raw)
                self.commands.append(command)
                reply = self._answer(command, websocket)
                if reply is not None:
                    await websocket.send(json.dumps(reply))
        except Exception:  # noqa: BLE001 - le faux serveur ne doit jamais faire échouer un test
            pass
        finally:
            if websocket in self.connections:
                self.connections.remove(websocket)
            self.subscribers = [entry for entry in self.subscribers if entry[0] is not websocket]

    def _answer(self, command: dict[str, Any], websocket: Any) -> dict[str, Any] | None:
        kind = command.get("type")
        message_id = command.get("id", 0)

        if kind == "subscribe_events":
            self.subscribers.append((websocket, message_id, command.get("event_type")))
            return {"id": message_id, "type": "result", "success": True, "result": None}

        if kind == "get_states":
            result: Any = self.states
        elif kind == "config/area_registry/list":
            result = self.areas
        elif kind == "config/device_registry/list":
            result = self.devices
        elif kind == "config/entity_registry/list":
            result = self.entities
        elif kind == "get_config":
            result = {
                "location_name": "Maison test",
                "version": HA_VERSION,
                "uuid": "core-uuid-1234",
            }
        elif kind == "auth/current_user":
            result = self.authenticated.get(websocket, {})
        elif kind == "call_service":
            result = {}
        else:
            return {
                "id": message_id,
                "type": "result",
                "success": False,
                "error": {"code": "unknown_command", "message": f"inconnue : {kind}"},
            }

        return {"id": message_id, "type": "result", "success": True, "result": result}


def build_cache() -> HaCache:
    """Cache prérempli avec les données du faux Home Assistant (sans réseau)."""
    cache = HaCache()
    cache._load(AREAS, DEVICES, ENTITIES, STATES, {"location_name": "Maison test"})
    return cache


class FakeHaServer:
    """Lance [FakeHomeAssistant] dans un thread dédié (usage synchrone)."""

    def __init__(self) -> None:
        self.fake = FakeHomeAssistant()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeHomeAssistant:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fake-ha", daemon=True
        )
        self._thread.start()
        self._run(self.fake.start())
        return self.fake

    def __exit__(self, *_: Any) -> bool:
        try:
            self._run(self.fake.stop())
        finally:
            assert self._loop is not None and self._thread is not None
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            self._loop.close()
        return False

    def _run(self, coroutine: Any) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(10)


def make_settings(root: Path, fake: FakeHomeAssistant, **overrides: Any) -> Settings:
    """Réglages pointant vers le faux Home Assistant et un dossier de données temporaire."""
    values: dict[str, Any] = {
        "data_dir": Path(root),
        "web_dir": Path(__file__).resolve().parents[1] / "web",
        "schema_dir": Path(__file__).resolve().parents[1] / "schema",
        "supervisor_token": FakeHomeAssistant.supervisor_token,
        "core_ws_url": fake.supervisor_url,
        "auth_ws_url": fake.auth_url,
        "ha_uuid": "ha-uuid-de-test",
        "history_limit": 20,
    }
    values.update(overrides)
    return Settings(**values)


def wait_until_sync(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition non atteinte dans le délai imparti")


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition non atteinte dans le délai imparti")
