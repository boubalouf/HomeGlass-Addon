"""Appareils Android connus et profils de mise en page.

Un profil par défaut s'applique à tous les appareils ; chaque appareil peut le surcharger
(masquer des pièces ou des onglets, forcer le mode clair/sombre). Les surcharges sont
résolues côté serveur : l'application reçoit toujours un document layout v1 complet.
"""

from __future__ import annotations

import copy
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from .atomic import read_json, write_json_atomic
from .repository import utc_now_string
from ..models.api import ProfileOverrides

logger = logging.getLogger(__name__)

#: Identifiant spécial du profil par défaut (aucun appareil ne peut le prendre).
DEFAULT_PROFILE_ID = "default"
_DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EMPTY_PROFILE: dict[str, Any] = {"hiddenRooms": [], "hiddenTabs": [], "themeMode": None}


class DeviceStore:
    """Registre des appareils et de leurs profils (fichier JSON unique)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # --------------------------------------------------------------------- lecture

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        if not isinstance(data, dict):
            data = {}
        devices = data.get("devices")
        profiles = data.get("profiles")
        return {
            "devices": devices if isinstance(devices, dict) else {},
            "profiles": profiles if isinstance(profiles, dict) else {},
        }

    def _save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data)

    def list_devices(self) -> list[dict[str, Any]]:
        data = self._load()
        devices = []
        for device_id, entry in sorted(data["devices"].items()):
            devices.append(
                {
                    "deviceId": device_id,
                    "name": entry.get("name"),
                    "model": entry.get("model"),
                    "appVersion": entry.get("appVersion"),
                    "androidVersion": entry.get("androidVersion"),
                    "firstSeen": entry.get("firstSeen"),
                    "lastSeen": entry.get("lastSeen"),
                    "profile": self._merged_profile(data, device_id),
                }
            )
        return devices

    def count(self) -> int:
        return len(self._load()["devices"])

    def has_device(self, device_id: str) -> bool:
        return device_id in self._load()["devices"]

    # ------------------------------------------------------------------ écritures

    def register(
        self,
        device_id: str | None = None,
        *,
        name: str | None = None,
        model: str | None = None,
        app_version: str | None = None,
        android_version: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Enregistre (ou met à jour) un appareil et renvoie son entrée."""
        identifier = (device_id or "").strip() or f"dev_{secrets.token_hex(4)}"
        if not _DEVICE_ID_PATTERN.match(identifier) or identifier == DEFAULT_PROFILE_ID:
            raise ValueError(
                "deviceId invalide : minuscules, chiffres, tirets et soulignés (64 max)"
            )

        stamp = now or utc_now_string()
        with self._lock:
            data = self._load()
            entry = data["devices"].get(identifier) or {}
            entry.setdefault("firstSeen", stamp)
            entry["lastSeen"] = stamp
            for key, value in (
                ("name", name),
                ("model", model),
                ("appVersion", app_version),
                ("androidVersion", android_version),
            ):
                if value:
                    entry[key] = value
            data["devices"][identifier] = entry
            self._save(data)
        logger.info("Appareil enregistré : %s", identifier)
        return {"deviceId": identifier, **entry}

    def profile_for(self, device_id: str | None) -> dict[str, Any]:
        """Surcharges effectives d'un appareil (profil par défaut inclus)."""
        data = self._load()
        return self._merged_profile(data, device_id)

    def set_profile(self, device_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
        """Enregistre les surcharges d'un appareil, ou du profil par défaut."""
        validated = ProfileOverrides.model_validate(overrides).model_dump(
            by_alias=True, mode="json", exclude_none=False
        )
        if device_id != DEFAULT_PROFILE_ID and not _DEVICE_ID_PATTERN.match(device_id):
            raise ValueError(f"deviceId invalide : {device_id!r}")
        with self._lock:
            data = self._load()
            data["profiles"][device_id] = validated
            self._save(data)
        logger.info("Profil mis à jour : %s", device_id)
        return validated

    def default_profile(self) -> dict[str, Any]:
        data = self._load()
        stored = data["profiles"].get(DEFAULT_PROFILE_ID) or {}
        return {**_EMPTY_PROFILE, **{k: v for k, v in stored.items() if v not in (None, [])}}

    # ------------------------------------------------------------------ résolution

    def _merged_profile(self, data: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        merged = dict(_EMPTY_PROFILE)
        for key in (DEFAULT_PROFILE_ID, device_id):
            stored = data["profiles"].get(key) if key else None
            if not isinstance(stored, dict):
                continue
            for field, value in stored.items():
                # Une valeur vide ne doit pas écraser le profil par défaut.
                if value:
                    merged[field] = value
        return merged

    def apply_profile(self, document: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        """Applique les surcharges d'un appareil à une copie du document publié."""
        profile = self.profile_for(device_id)
        resolved = copy.deepcopy(document)

        hidden_rooms = set(profile.get("hiddenRooms") or [])
        if hidden_rooms:
            resolved["rooms"] = [
                room for room in resolved.get("rooms") or [] if room.get("id") not in hidden_rooms
            ]

        hidden_tabs = set(profile.get("hiddenTabs") or [])
        if hidden_tabs:
            resolved["tabs"] = [
                {**tab, "enabled": False} if tab.get("id") in hidden_tabs else tab
                for tab in resolved.get("tabs") or []
            ]

        mode = profile.get("themeMode")
        if mode:
            resolved.setdefault("theme", {})["mode"] = mode
        return resolved


__all__ = ["DEFAULT_PROFILE_ID", "DeviceStore"]
