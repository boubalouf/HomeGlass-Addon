"""Cache mémoire de l'état de Home Assistant.

Le cache est alimenté à chaque (re)connexion par les registres de Core (pièces, appareils,
entités) et par ``get_states``, puis maintenu à jour par deux abonnements :

* ``state_changed`` met à jour l'état et les attributs d'une entité ;
* ``area_registry_updated``, ``device_registry_updated`` et ``entity_registry_updated``
  déclenchent un rechargement complet des registres (ces événements sont rares et le
  rechargement garantit la cohérence des associations entité → appareil → pièce).

Aucune écriture disque : le cache est reconstruit au démarrage, Home Assistant restant la
source de vérité.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import HaWebSocketClient

logger = logging.getLogger(__name__)

#: Domaines dont les événements de registre imposent un rechargement complet.
REGISTRY_EVENTS = (
    "area_registry_updated",
    "device_registry_updated",
    "entity_registry_updated",
)


@dataclass(slots=True)
class AreaRecord:
    area_id: str
    name: str
    icon: str | None = None
    floor_id: str | None = None


@dataclass(slots=True)
class DeviceRecord:
    id: str
    name: str | None = None
    area_id: str | None = None
    manufacturer: str | None = None
    model: str | None = None


@dataclass(slots=True)
class EntityRecord:
    entity_id: str
    name: str
    domain: str
    device_id: str | None = None
    area_id: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    disabled: bool = False
    hidden: bool = False
    platform: str | None = None
    state: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def object_id(self) -> str:
        return self.entity_id.split(".", 1)[-1]

    @property
    def device_class(self) -> str | None:
        value = self.attributes.get("device_class")
        return value if isinstance(value, str) else None

    @property
    def supported_features(self) -> int:
        value = self.attributes.get("supported_features")
        return value if isinstance(value, int) else 0


class HaCache:
    """Instantané en lecture seule de Home Assistant, tenu à jour par événements."""

    def __init__(self) -> None:
        self.areas: dict[str, AreaRecord] = {}
        self.devices: dict[str, DeviceRecord] = {}
        self.entities: dict[str, EntityRecord] = {}
        self.location_name: str | None = None
        self.revision = 0
        self.loaded_at: datetime | None = None
        self._client: HaWebSocketClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ alimentation

    async def attach(self, client: HaWebSocketClient) -> None:
        """Rappel ``on_ready`` du client : charge les données puis s'abonne."""
        self._client = client
        await self.refresh(client)
        for event_type, handler in (
            ("state_changed", self._on_state_changed),
            *((event_type, self._on_registry_updated) for event_type in REGISTRY_EVENTS),
        ):
            await client.subscribe_events(event_type, handler)
        logger.info(
            "Cache Home Assistant chargé : %d entités, %d pièces, %d appareils",
            len(self.entities),
            len(self.areas),
            len(self.devices),
        )

    async def refresh(self, client: HaWebSocketClient) -> None:
        """Recharge intégralement les registres et les états."""
        async with self._lock:
            areas = await client.command("config/area_registry/list")
            devices = await client.command("config/device_registry/list")
            entities = await client.command("config/entity_registry/list")
            states = await client.command("get_states")
            try:
                config = await client.command("get_config")
            except Exception as error:  # noqa: BLE001 - le nom de la maison est accessoire
                logger.debug("get_config indisponible : %s", error)
                config = {}
            self._load(areas, devices, entities, states, config)

    def _load(
        self,
        areas: Any,
        devices: Any,
        entities: Any,
        states: Any,
        config: Any = None,
    ) -> None:
        self.areas = {
            area["area_id"]: AreaRecord(
                area_id=area["area_id"],
                name=str(area.get("name") or area["area_id"]),
                icon=area.get("icon"),
                floor_id=area.get("floor_id"),
            )
            for area in areas or []
            if isinstance(area, dict) and area.get("area_id")
        }
        self.devices = {
            device["id"]: DeviceRecord(
                id=device["id"],
                name=device.get("name_by_user") or device.get("name"),
                area_id=device.get("area_id"),
                manufacturer=device.get("manufacturer"),
                model=device.get("model"),
            )
            for device in devices or []
            if isinstance(device, dict) and device.get("id")
        }

        state_map = {
            state["entity_id"]: state
            for state in states or []
            if isinstance(state, dict) and state.get("entity_id")
        }

        records: dict[str, EntityRecord] = {}
        for entry in entities or []:
            if not isinstance(entry, dict) or not entry.get("entity_id"):
                continue
            entity_id = entry["entity_id"]
            state = state_map.get(entity_id) or {}
            attributes = state.get("attributes")
            attributes = dict(attributes) if isinstance(attributes, dict) else {}
            records[entity_id] = EntityRecord(
                entity_id=entity_id,
                name=str(
                    entry.get("name")
                    or entry.get("original_name")
                    or attributes.get("friendly_name")
                    or entity_id
                ),
                domain=entity_id.split(".", 1)[0],
                device_id=entry.get("device_id"),
                area_id=entry.get("area_id"),
                icon=entry.get("icon"),
                entity_category=entry.get("entity_category"),
                disabled=bool(entry.get("disabled_by")),
                hidden=bool(entry.get("hidden_by")),
                platform=entry.get("platform"),
                state=str(state.get("state", "unknown")),
                attributes=attributes,
            )

        if isinstance(config, dict):
            location = config.get("location_name")
            if isinstance(location, str) and location.strip():
                self.location_name = location.strip()

        self.entities = records
        self.revision += 1
        self.loaded_at = datetime.now(timezone.utc)

    # --------------------------------------------------------------------- événements

    def _on_state_changed(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        record = self.entities.get(entity_id) if isinstance(entity_id, str) else None
        if record is None:
            return
        new_state = data.get("new_state")
        if isinstance(new_state, dict):
            record.state = str(new_state.get("state", "unknown"))
            attributes = new_state.get("attributes")
            record.attributes = dict(attributes) if isinstance(attributes, dict) else {}
        else:
            # ``new_state`` nul : l'entité vient d'être supprimée de l'état courant.
            record.state = "unavailable"
            record.attributes = {}
        self.revision += 1

    async def _on_registry_updated(self, event: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            return
        logger.debug("Registre Home Assistant modifié (%s) : rechargement", event.get("event_type"))
        try:
            await self.refresh(client)
        except Exception as error:  # noqa: BLE001 - la reconnexion rechargera de toute façon
            logger.warning("Rechargement des registres impossible : %s", error)

    # ------------------------------------------------------------------- accesseurs

    def area_for(self, record: EntityRecord) -> str | None:
        """Pièce d'une entité : la sienne, sinon celle de son appareil."""
        if record.area_id and record.area_id in self.areas:
            return record.area_id
        device = self.devices.get(record.device_id) if record.device_id else None
        if device and device.area_id and device.area_id in self.areas:
            return device.area_id
        return None

    def entity_snapshot(self) -> list[dict[str, Any]]:
        """Liste d'entités enrichie, destinée à l'éditeur web."""
        snapshot = []
        for record in sorted(self.entities.values(), key=lambda item: item.entity_id):
            area_id = self.area_for(record)
            area = self.areas.get(area_id) if area_id else None
            snapshot.append(
                {
                    "entityId": record.entity_id,
                    "name": record.name,
                    "domain": record.domain,
                    "state": record.state,
                    "areaId": area_id,
                    "areaName": area.name if area else None,
                    "deviceId": record.device_id,
                    "deviceClass": record.device_class,
                    "icon": record.icon,
                    "entityCategory": record.entity_category,
                    "supportedFeatures": record.supported_features,
                    "unit": record.attributes.get("unit_of_measurement"),
                }
            )
        return snapshot

    def area_snapshot(self) -> list[dict[str, Any]]:
        """Pièces avec le nombre d'entités qui leur sont rattachées."""
        counts = Counter(
            area_id
            for record in self.entities.values()
            if (area_id := self.area_for(record)) is not None
        )
        return [
            {
                "areaId": area.area_id,
                "name": area.name,
                "icon": area.icon,
                "floorId": area.floor_id,
                "entityCount": counts.get(area.area_id, 0),
            }
            for area in sorted(self.areas.values(), key=lambda item: item.name.lower())
        ]


__all__ = [
    "AreaRecord",
    "DeviceRecord",
    "EntityRecord",
    "HaCache",
    "REGISTRY_EVENTS",
]
