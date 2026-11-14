"""Diffusion de ``layout_updated`` vers les applications connectées."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SocketLike(Protocol):
    async def send_json(self, data: Any) -> None: ...


class LayoutHub:
    """Registre des WebSockets ``/api/ws`` abonnés aux changements de mise en page."""

    def __init__(self) -> None:
        self._sockets: dict[SocketLike, dict[str, Any]] = {}

    def register(self, socket: SocketLike, *, device_id: str | None = None) -> None:
        self._sockets[socket] = {"deviceId": device_id}
        logger.debug("WebSocket enregistré (%d abonnés)", len(self._sockets))

    def unregister(self, socket: SocketLike) -> None:
        self._sockets.pop(socket, None)

    def __len__(self) -> int:
        return len(self._sockets)

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Envoie ``message`` à tous les abonnés ; renvoie le nombre d'envois réussis."""
        sockets = list(self._sockets)
        if not sockets:
            return 0
        results = await asyncio.gather(
            *(self._send(socket, message) for socket in sockets),
            return_exceptions=True,
        )
        delivered = 0
        for socket, result in zip(sockets, results):
            if isinstance(result, BaseException):
                logger.debug("Envoi WebSocket abandonné (%s) : %s", type(result).__name__, result)
                self._sockets.pop(socket, None)
            else:
                delivered += 1
        return delivered

    async def _send(self, socket: SocketLike, message: dict[str, Any]) -> None:
        await socket.send_json(message)


__all__ = ["LayoutHub", "SocketLike"]
