"""Limitation de débit simple des routes du port API (8765).

Le port Ingress n'est pas limité : il est déjà protégé par la session Home Assistant et
sert l'éditeur, qui émet des rafales de requêtes légitimes. Le port API, lui, est exposé au
réseau local ; la clé associe l'IP source et l'empreinte du jeton pour qu'un appareil mal
emballé ne pénalise pas les autres.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)


class RateLimiter:
    """Fenêtre glissante en mémoire, une file d'horodatages par clé."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = int(limit)
        self.window = float(window)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._calls_since_prune = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def hit(self, key: str) -> float | None:
        """Enregistre une requête ; renvoie le délai à attendre si la limite est atteinte."""
        if not self.enabled:
            return None
        now = self._clock()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return max(0.0, self.window - (now - bucket[0]))
        bucket.append(now)
        self._calls_since_prune += 1
        if self._calls_since_prune >= 256:
            self._calls_since_prune = 0
            self._prune(now)
        return None

    def reset(self) -> None:
        self._hits.clear()

    def _prune(self, now: float) -> None:
        for key in [key for key, bucket in self._hits.items() if not bucket or now - bucket[-1] > self.window]:
            self._hits.pop(key, None)


class RateLimitMiddleware:
    """Applique [RateLimiter] aux seules requêtes arrivant sur le port API."""

    def __init__(self, app: Any, *, limiter: RateLimiter, api_port: int | None) -> None:
        self.app = app
        self.limiter = limiter
        self.api_port = api_port

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        retry_after = self.limiter.hit(self._key(scope))
        if retry_after is None:
            await self.app(scope, receive, send)
            return

        client = (scope.get("client") or ("?", 0))[0]
        logger.warning(
            "Limite de débit atteinte pour %s sur %s : %d requêtes / %.0f s",
            client,
            scope.get("path"),
            self.limiter.limit,
            self.limiter.window,
        )
        body = json.dumps({"detail": "Trop de requêtes"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(max(1, int(retry_after) + 1)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _applies(self, scope: dict) -> bool:
        if not self.limiter.enabled or self.api_port is None:
            return False
        if scope.get("type") != "http":
            return False
        server = scope.get("server") or ("", None)
        return server[1] == self.api_port

    @staticmethod
    def _key(scope: dict) -> str:
        client = (scope.get("client") or ("?", 0))[0]
        token = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                token = hashlib.sha256(value).hexdigest()[:12]
                break
        return f"{client}|{token}"


__all__ = ["RateLimitMiddleware", "RateLimiter"]
