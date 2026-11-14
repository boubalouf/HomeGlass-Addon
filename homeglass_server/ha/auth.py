"""Validation des jetons de l'application Android auprès de Home Assistant.

Le jeton Bearer présenté par l'application est un jeton d'accès longue durée de Home
Assistant. On l'accepte en ouvrant une connexion WebSocket vers Core *avec ce jeton*
(``ws://homeassistant:8123/api/websocket``), puis en demandant ``auth/current_user`` :
c'est la même validation que celle de Home Assistant, sans réimplémenter JWT ni base de
données. Le résultat est mis en cache 5 minutes, indexé par le SHA-256 du jeton (jamais
le jeton en clair).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)

TOKEN_TTL = 300.0
AUTH_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class HaUser:
    """Utilisateur Home Assistant authentifié."""

    id: str
    name: str = ""
    is_admin: bool = False
    is_owner: bool = False
    #: ``token`` (jeton Bearer) ou ``ingress`` (session Home Assistant déjà authentifiée).
    source: str = "token"


class TokenValidator:
    """Vérifie un jeton d'accès auprès de Core, avec cache en mémoire."""

    def __init__(
        self,
        url: str,
        *,
        ttl: float = TOKEN_TTL,
        timeout: float = AUTH_TIMEOUT,
        connect_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.ttl = ttl
        self.timeout = timeout
        self._connect_factory = connect_factory or connect
        self._clock = clock
        self._cache: dict[str, tuple[float, HaUser | None]] = {}

    async def validate(self, token: str) -> HaUser | None:
        """Renvoie l'utilisateur, ou None si le jeton est invalide ou Core injoignable."""
        token = (token or "").strip()
        if not token:
            return None

        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        user = await self._fetch(token)
        self._cache[key] = (now + self.ttl, user)
        self._prune(now)
        return user

    def forget(self) -> None:
        self._cache.clear()

    async def _fetch(self, token: str) -> HaUser | None:
        try:
            async with self._connect_factory(
                self.url,
                open_timeout=self.timeout,
                close_timeout=1,
            ) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), self.timeout))
                if hello.get("type") != "auth_required":
                    logger.warning(
                        "Réponse inattendue de %s à la connexion : %r", self.url, hello.get("type")
                    )
                    return None

                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), self.timeout))
                if reply.get("type") == "auth_invalid":
                    logger.info(
                        "Jeton refusé par Home Assistant : %s", reply.get("message") or "invalide"
                    )
                    return None
                if reply.get("type") != "auth_ok":
                    logger.warning("Réponse d'authentification inattendue : %r", reply.get("type"))
                    return None

                await ws.send(json.dumps({"id": 1, "type": "auth/current_user"}))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), self.timeout))
                    if message.get("type") != "result" or message.get("id") != 1:
                        continue
                    if not message.get("success"):
                        logger.warning("auth/current_user a échoué : %r", message.get("error"))
                        return None
                    result = message.get("result") or {}
                    return HaUser(
                        id=str(result.get("id") or "unknown"),
                        name=str(result.get("name") or ""),
                        is_admin=bool(result.get("is_admin")),
                        is_owner=bool(result.get("is_owner")),
                    )
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as error:
            logger.warning(
                "Validation impossible : Home Assistant est injoignable sur %s (%s). "
                "Vérifiez que Core est démarré et que HG_AUTH_WS_URL est correct.",
                self.url,
                error,
            )
            return None
        except Exception as error:  # noqa: BLE001 - erreur de poignée de main WebSocket
            logger.warning(
                "Validation impossible : connexion WebSocket à %s en échec (%s : %s)",
                self.url,
                type(error).__name__,
                error,
            )
            return None

    def _prune(self, now: float) -> None:
        expired = [key for key, (expiry, _) in self._cache.items() if expiry <= now]
        for key in expired:
            self._cache.pop(key, None)


__all__ = ["HaUser", "TokenValidator"]
