"""Tolérance de routage derrière Ingress et diagnostics HTTP bas niveau.

Home Assistant monte l'add-on derrière ``/api/hassio_ingress/<token>/``. Selon la version du
Supervisor et la façon dont l'interface est ouverte, le proxy retire ce préfixe… ou pas, et
peut transmettre un chemin vide, ``//`` ou ``/index.html``. Ce module normalise le chemin
avant le routage et journalise ce que le proxy a réellement envoyé.

Il fournit aussi un protocole h11 instrumenté : quand uvicorn signale « Invalid HTTP request
received », on veut savoir *qui* a parlé et *avec quels octets* (TLS sur un port HTTP,
préface HTTP/2, sonde de santé…).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable

from uvicorn.protocols.http.h11_impl import H11Protocol

logger = logging.getLogger(__name__)

#: Préfixes des sessions Ingress : /api/hassio_ingress/<token> (Home Assistant Core) et
#: /ingress/<token> (forme interne du Supervisor). Le proxy les retire normalement ; on les
#: tolère au cas où il ne le ferait pas.
INGRESS_PREFIX_RE = re.compile(r"^/(?:api/hassio_ingress|ingress)/[^/]+")
_DUPLICATE_SLASHES_RE = re.compile(r"/{2,}")

#: Préfixes réservés : ces chemins ne doivent jamais renvoyer la page d'accueil.
RESERVED_PREFIXES = frozenset({"api", "internal", "assets"})

_HEX_LIMIT = 64


def normalize_path(path: str) -> tuple[str, list[str]]:
    """Ramène un chemin reçu du proxy à la forme attendue par le routeur.

    Renvoie le chemin normalisé et la liste des réécritures appliquées (vide si le chemin
    était déjà exploitable).
    """
    if path == "*":
        # Forme astérisque (``OPTIONS *``, préface HTTP/2) : ce n'est pas un chemin, on la
        # transmet telle quelle plutôt que d'inventer « /* ».
        return path, []

    notes: list[str] = []
    original = path

    if not path.startswith("/"):
        path = "/" + path
        notes.append("barre oblique initiale ajoutée")

    prefix = INGRESS_PREFIX_RE.match(path)
    if prefix:
        path = path[prefix.end() :] or "/"
        notes.append(f"préfixe Ingress retiré ({prefix.group(0)})")

    collapsed = _DUPLICATE_SLASHES_RE.sub("/", path)
    if collapsed != path:
        notes.append("barres obliques dupliquées réduites")
        path = collapsed

    if not path:
        path = "/"
        notes.append("chemin vide remplacé par /")

    if notes and path == original:
        notes.clear()
    return path, notes


def to_hex(data: bytes, limit: int = _HEX_LIMIT) -> str:
    """Premiers octets d'un flux en hexadécimal, tronqués lisiblement."""
    chunk = data[:limit]
    text = " ".join(f"{byte:02x}" for byte in chunk)
    if len(data) > limit:
        text += f" … ({len(data)} octets reçus)"
    return text


def describe_request_bytes(data: bytes) -> str:
    """Interprète les premiers octets d'une requête refusée par le parseur HTTP."""
    if not data:
        return "aucune donnée : connexion ouverte puis fermée (sonde TCP ou contrôle de santé)"

    if data[:1] == b"\x80" or data[:2] in {b"\x16\x01", b"\x16\x02", b"\x16\x03"}:
        return (
            "ClientHello TLS : un client parle HTTPS à un port HTTP "
            "(URL en https://, proxy ou favori erroné)"
        )

    if data.startswith(b"PRI * HTTP/2.0"):
        return "préface HTTP/2 en clair (h2c) : ce port ne sert que HTTP/1.1"

    printable = sum(1 for byte in data[:8] if 32 <= byte < 127)
    if printable < 4:
        return "flux binaire : ni HTTP ni TLS (mauvais port, ou protocole propriétaire)"

    request_line = data.split(b"\r\n", 1)[0]
    if request_line.startswith(b"HTTP/"):
        return "ligne de statut reçue au lieu d'une ligne de requête (client et serveur inversés)"
    if not request_line.strip():
        return "ligne de requête vide"
    if request_line.split(b" ", 1)[0] in {b"*", b""}:
        return "cible de requête invalide (« * » ou vide)"
    if b"HTTP/" in request_line:
        return "requête HTTP/1.x malformée (en-têtes ou ligne de requête invalides)"

    return "ligne de requête illisible (protocole non HTTP)"


class IngressPathMiddleware:
    """Middleware ASGI : normalise le chemin Ingress avant le routage, et le journalise."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_path: str = scope.get("path", "")
        normalized, notes = normalize_path(raw_path)
        headers = dict(scope.get("headers") or [])
        ingress_path = headers.get(b"x-ingress-path", b"").decode("latin-1")
        client = scope.get("client") or ("inconnu", 0)
        method = scope.get("method", "?")

        details = [f"client {client[0]}:{client[1]}"]
        if ingress_path:
            details.append(f"X-Ingress-Path {ingress_path}")
        if normalized != raw_path:
            details.append(f"brut {raw_path!r}")
        logger.info("%s %s — %s", method, normalized, " | ".join(details))

        if notes:
            logger.warning(
                "Chemin Ingress réécrit : %r → %r (%s)", raw_path, normalized, ", ".join(notes)
            )

        if normalized != raw_path:
            scope = dict(scope)
            scope["path"] = normalized
            scope["raw_path"] = normalized.encode("utf-8")

        await self.app(scope, receive, send)


class ObservingH11Protocol(H11Protocol):
    """Protocole h11 qui explique les « Invalid HTTP request received ».

    uvicorn se contente d'un avertissement générique ; on ajoute l'adresse du client, la
    classification du flux et ses premiers octets en hexadécimal.
    """

    _seen_bytes: bytes = b""

    def connection_made(self, transport: Any) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._seen_bytes = b""

    def data_received(self, data: bytes) -> None:
        if len(self._seen_bytes) < 256:
            self._seen_bytes = (self._seen_bytes + data)[:256]
        super().data_received(data)

    def connection_lost(self, exc: Exception | None) -> None:  # type: ignore[override]
        if not self._seen_bytes:
            peer = f"{self.client[0]}:{self.client[1]}" if self.client else "client inconnu"
            logger.debug(
                "Connexion fermée sans données (%s) : sonde TCP ou contrôle de santé", peer
            )
        super().connection_lost(exc)

    def send_400_response(self, msg: str) -> None:
        peer = f"{self.client[0]}:{self.client[1]}" if self.client else "client inconnu"
        logger.warning(
            "Requête HTTP invalide de %s — %s | premiers octets : %s",
            peer,
            describe_request_bytes(self._seen_bytes),
            to_hex(self._seen_bytes),
        )
        super().send_400_response(msg)


def is_reserved_path(full_path: str) -> bool:
    """Vrai si le chemin appartient à un espace réservé (api, internal, assets)."""
    first_segment = full_path.split("/", 1)[0]
    return first_segment in RESERVED_PREFIXES
