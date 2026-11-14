"""Appairage de l'application Android sans jeton Home Assistant.

L'application annonce sa présence (``POST /api/pair/request``) ; l'add-on crée une demande en
attente et **affiche le code à six chiffres dans son panneau Home Assistant**. Le code n'est
jamais renvoyé à l'application : il faut le lire dans Home Assistant, donc être déjà autorisé
sur l'instance. L'application échange ensuite ``pairingId`` + code contre un jeton d'appareil,
dont seule l'empreinte SHA-256 est conservée sur le disque.

Le fichier reste petit et lisible : demandes en attente (durée de vie courte) et appareils
appairés (jeton haché, jamais en clair).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .atomic import read_json, write_json_atomic

logger = logging.getLogger(__name__)

#: Durée de vie d'un code d'appairage (10 minutes).
PAIRING_TTL_SECONDS = 600.0
#: Demandes simultanées conservées (la plus ancienne est oubliée au-delà).
MAX_PENDING = 5
#: Tentatives de saisie du code avant annulation de la demande.
MAX_ATTEMPTS = 5
#: Longueur du code affiché dans Home Assistant.
CODE_LENGTH = 6

_DEVICE_ID_PREFIX = "dev_"
_DEVICE_PUBLIC_KEYS = (
    "deviceId",
    "name",
    "model",
    "appVersion",
    "address",
    "pairedAt",
    "lastSeenAt",
    "tokenHint",
)


class PairingError(Exception):
    """Échec d'appairage : code faux, demande inconnue, expirée ou verrouillée."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PairingStore:
    """Demandes d'appairage en attente et appareils appairés (fichier JSON unique)."""

    def __init__(
        self,
        path: Path,
        *,
        ttl: float = PAIRING_TTL_SECONDS,
        max_pending: int = MAX_PENDING,
        max_attempts: int = MAX_ATTEMPTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl = float(ttl)
        self.max_pending = max(1, int(max_pending))
        self.max_attempts = max(1, int(max_attempts))
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    # --------------------------------------------------------------------- lectures

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        if not isinstance(data, dict):
            data = {}
        pending = data.get("pending")
        devices = data.get("devices")
        return {
            "pending": pending if isinstance(pending, dict) else {},
            "devices": devices if isinstance(devices, dict) else {},
        }

    def _save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data)

    def pending(self) -> list[dict[str, Any]]:
        """Demandes en attente, code inclus : usage exclusif de l'interface Ingress."""
        now = self._clock()
        with self._lock:
            data = self._load()
            if self._prune(data, now):
                self._save(data)
            entries = sorted(
                data["pending"].values(),
                key=lambda entry: entry.get("createdAt") or "",
                reverse=True,
            )
        return [{**entry, "expiresIn": self._remaining(entry, now)} for entry in entries]

    def devices(self) -> list[dict[str, Any]]:
        """Appareils appairés, sans empreinte de jeton (affichage seulement)."""
        with self._lock:
            data = self._load()
        return [
            {key: device.get(key) for key in _DEVICE_PUBLIC_KEYS}
            for device in sorted(
                data["devices"].values(), key=lambda device: device.get("name") or ""
            )
        ]

    def count(self) -> int:
        with self._lock:
            return len(self._load()["devices"])

    # -------------------------------------------------------------------- écritures

    def request(
        self,
        *,
        device_name: str | None = None,
        model: str | None = None,
        app_version: str | None = None,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Crée une demande d'appairage ; renvoie l'entrée complète (code compris)."""
        now = self._clock()
        with self._lock:
            data = self._load()
            self._prune(data, now)
            while len(data["pending"]) >= self.max_pending:
                oldest = min(
                    data["pending"],
                    key=lambda key: data["pending"][key].get("createdAt") or "",
                )
                data["pending"].pop(oldest, None)

            entry = {
                "pairingId": secrets.token_urlsafe(9),
                "code": f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}",
                "deviceId": f"{_DEVICE_ID_PREFIX}{secrets.token_hex(4)}",
                "deviceName": (device_name or "").strip() or "HomeGlass",
                "model": (model or "").strip() or None,
                "appVersion": (app_version or "").strip() or None,
                "address": address,
                "createdAt": now.isoformat(),
                "expiresAt": (now + timedelta(seconds=self.ttl)).isoformat(),
                "attempts": 0,
            }
            data["pending"][entry["pairingId"]] = entry
            self._save(data)

        logger.info(
            "Demande d'appairage créée : %s (%s, %s)",
            entry["pairingId"],
            entry["deviceName"],
            address or "adresse inconnue",
        )
        return dict(entry)

    def claim(self, pairing_id: str, code: str) -> dict[str, Any]:
        """Échange un code contre un jeton d'appareil (usage unique)."""
        normalized = "".join(character for character in (code or "") if character.isdigit())
        now = self._clock()
        with self._lock:
            data = self._load()
            self._prune(data, now)
            entry = data["pending"].get(pairing_id)
            if entry is None:
                raise PairingError("demande d'appairage inconnue ou expirée", 404)

            attempts = int(entry.get("attempts") or 0) + 1
            if not hmac.compare_digest(str(entry.get("code") or ""), normalized):
                entry["attempts"] = attempts
                if attempts >= self.max_attempts:
                    data["pending"].pop(pairing_id, None)
                    self._save(data)
                    raise PairingError("trop de tentatives : relancez l'appairage", 429)
                self._save(data)
                raise PairingError("code d'appairage incorrect", 403)

            token = secrets.token_urlsafe(32)
            device = {
                "deviceId": entry["deviceId"],
                "name": entry.get("deviceName"),
                "model": entry.get("model"),
                "appVersion": entry.get("appVersion"),
                "address": entry.get("address"),
                "pairedAt": now.isoformat(),
                "lastSeenAt": now.isoformat(),
                "tokenHash": _digest(token),
                "tokenHint": token[-4:],
            }
            data["devices"][device["deviceId"]] = device
            data["pending"].pop(pairing_id, None)
            self._save(data)

        logger.info("Appareil appairé : %s (%s)", device["deviceId"], device.get("name"))
        return {"token": token, **device}

    def authenticate(self, token: str) -> dict[str, Any] | None:
        """Appareil correspondant à un jeton d'appairage, ou None."""
        token = (token or "").strip()
        if len(token) < 16:
            return None

        digest = _digest(token)
        now = self._clock()
        with self._lock:
            data = self._load()
            for device in data["devices"].values():
                if hmac.compare_digest(str(device.get("tokenHash") or ""), digest):
                    device["lastSeenAt"] = now.isoformat()
                    self._save(data)
                    return dict(device)
        return None

    def revoke(self, device_id: str) -> bool:
        """Retire un appareil appairé : son jeton cesse immédiatement de fonctionner."""
        with self._lock:
            data = self._load()
            if device_id not in data["devices"]:
                return False
            data["devices"].pop(device_id, None)
            self._save(data)
        logger.info("Appareil révoqué : %s", device_id)
        return True

    def cancel(self, pairing_id: str) -> bool:
        """Annule une demande en attente."""
        with self._lock:
            data = self._load()
            if pairing_id not in data["pending"]:
                return False
            data["pending"].pop(pairing_id, None)
            self._save(data)
        logger.info("Demande d'appairage annulée : %s", pairing_id)
        return True

    # ---------------------------------------------------------------------- interne

    def _prune(self, data: dict[str, Any], now: datetime) -> bool:
        expired = [
            key for key, entry in data["pending"].items() if not self._still_valid(entry, now)
        ]
        for key in expired:
            data["pending"].pop(key, None)
        return bool(expired)

    @staticmethod
    def _still_valid(entry: dict[str, Any], now: datetime) -> bool:
        try:
            expires = datetime.fromisoformat(str(entry.get("expiresAt")))
        except (TypeError, ValueError):
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now

    @staticmethod
    def _remaining(entry: dict[str, Any], now: datetime) -> int:
        try:
            expires = datetime.fromisoformat(str(entry.get("expiresAt")))
        except (TypeError, ValueError):
            return 0
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return max(0, int((expires - now).total_seconds()))


__all__ = [
    "CODE_LENGTH",
    "MAX_ATTEMPTS",
    "MAX_PENDING",
    "PAIRING_TTL_SECONDS",
    "PairingError",
    "PairingStore",
]
