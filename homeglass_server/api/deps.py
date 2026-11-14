"""Dépendances FastAPI : authentification, distinction Ingress / port API.

Deux chemins d'accès coexistent :

* **port Ingress (8099)** : Home Assistant Core a déjà authentifié l'utilisateur avant de
  proxifier la requête. Aucun jeton n'est redemandé ; les routes ``/internal/*`` y sont
  réservées.
* **port API (8765)** : l'application Android présente un jeton Bearer validé auprès de
  Core ; les routes ``/internal/*`` y répondent 404 (elles n'existent pas pour ce port).

Le port d'écoute réel est lu dans ``scope["server"]`` : uvicorn le renseigne par socket,
il ne peut donc pas être falsifié par un en-tête. C'est ce qui rend la séparation fiable.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import Depends, HTTPException, Request, status

from ..ha.auth import HaUser

INGRESS_USER_ID = "ingress"
BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def headers_from_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    """En-têtes ASGI (octets) vers dictionnaire minuscule."""
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers") or []
    }


def is_ingress_request(scope: Mapping[str, Any], ingress_port: int | None) -> bool:
    """Vrai si la requête arrive sur le port Ingress."""
    if ingress_port is None:
        return False
    server = scope.get("server") or ("", None)
    return server[1] == ingress_port


def ingress_user(headers: Mapping[str, str]) -> HaUser:
    """Utilisateur déduit des en-têtes posés par le proxy d'Ingress de Core."""
    user_id = headers.get("x-remote-user-id") or INGRESS_USER_ID
    name = (
        headers.get("x-remote-user-display-name")
        or headers.get("x-remote-user-name")
        or "Home Assistant"
    )
    return HaUser(id=user_id, name=name, is_admin=False, is_owner=False, source="ingress")


def bearer_token(headers: Mapping[str, str]) -> str | None:
    authorization = headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


async def authenticate_token(state: Any, token: str) -> HaUser | None:
    """Utilisateur associé à un jeton : appareil appairé, sinon jeton Home Assistant.

    Les jetons d'appairage sont vérifiés localement (empreinte SHA-256), sans appel réseau ;
    les jetons longue durée de Home Assistant restent acceptés pour les installations qui
    n'utilisent pas l'appairage.
    """
    token = (token or "").strip()
    if not token:
        return None

    device = state.pairing.authenticate(token)
    if device is not None:
        return HaUser(
            id=f"device:{device['deviceId']}",
            name=str(device.get("name") or ""),
            is_admin=False,
            is_owner=False,
            source="pairing",
        )
    return await state.tokens.validate(token)


async def current_user(request: Request) -> HaUser:
    """Utilisateur courant : session Ingress, appareil appairé ou jeton Home Assistant."""
    settings = request.app.state.settings
    if is_ingress_request(request.scope, settings.ingress_port):
        return ingress_user(headers_from_scope(request.scope))

    token = bearer_token(headers_from_scope(request.scope))
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Jeton Bearer requis",
            headers=BEARER_CHALLENGE,
        )
    user = await authenticate_token(request.app.state, token)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Jeton invalide ou expiré",
            headers=BEARER_CHALLENGE,
        )
    return user


async def require_admin(user: HaUser = Depends(current_user)) -> HaUser:
    """Exige un administrateur Home Assistant (403 sinon)."""
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Administrateur Home Assistant requis",
        )
    return user


async def require_ingress(request: Request) -> None:
    """Réserve une route au port Ingress ; 404 sur le port API (route inexistante)."""
    settings = request.app.state.settings
    if not is_ingress_request(request.scope, settings.ingress_port):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")


__all__ = [
    "authenticate_token",
    "bearer_token",
    "current_user",
    "headers_from_scope",
    "ingress_user",
    "is_ingress_request",
    "require_admin",
    "require_ingress",
]
