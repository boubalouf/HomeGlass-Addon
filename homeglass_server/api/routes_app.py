"""Routes de l'application Android : mise en page, ressources, WebSocket.

Toutes ces routes exigent un jeton Bearer (sauf ``/api/ping``, déclaré ailleurs). Quand la
requête arrive par Ingress, la session Home Assistant fait office d'authentification et
aucun jeton n'est redemandé.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

from ..ha.auth import HaUser
from ..models.api import DeviceRegisterRequest, ProfileOverrides, PublicUser, RegisterResponse
from ..store.assets import AssetError
from .deps import (
    authenticate_token,
    current_user,
    headers_from_scope,
    ingress_user,
    is_ingress_request,
)
from .service import ensure_published, etag_matches, layout_etag, notify_layout_updated

logger = logging.getLogger(__name__)

router = APIRouter(tags=["app"])

#: Délai laissé à l'application pour envoyer son premier message d'authentification.
SOCKET_AUTH_TIMEOUT = 10.0
#: Fréquence des ``ping`` envoyés aux applications pour détecter une connexion morte.
SOCKET_PING_INTERVAL = 30.0


@router.post(
    "/api/devices/register",
    response_model=RegisterResponse,
    response_model_by_alias=True,
    summary="Enregistre un appareil Android et renvoie son profil",
)
async def register_device(
    payload: DeviceRegisterRequest,
    request: Request,
    user: HaUser = Depends(current_user),
) -> RegisterResponse:
    state = request.app.state
    try:
        device = state.devices.register(
            payload.device_id,
            name=payload.name,
            model=payload.model,
            app_version=payload.app_version,
            android_version=payload.android_version,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

    # Une application qui se connecte avant toute publication déclenche la génération
    # automatique : elle ne verra jamais un dashboard vide.
    document, created = ensure_published(state)
    if created:
        await notify_layout_updated(state, document["revision"])

    logger.info("Appareil %s enregistré (utilisateur %s)", device["deviceId"], user.id)
    return RegisterResponse(
        device_id=device["deviceId"],
        revision=int(document.get("revision") or 1),
        profile=ProfileOverrides.model_validate(state.devices.profile_for(device["deviceId"])),
    )


@router.get(
    "/api/layout",
    summary="Mise en page résolue pour un appareil (ETag, 304 si inchangée)",
)
async def get_layout(request: Request, user: HaUser = Depends(current_user)) -> Response:
    state = request.app.state
    device_id = request.query_params.get("device_id") or None

    document, created = ensure_published(state)
    if created:
        await notify_layout_updated(state, document["revision"])

    resolved = state.devices.apply_profile(document, device_id)
    etag = layout_etag(resolved)
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Authorization"}

    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    body = json.dumps(resolved, ensure_ascii=False)
    logger.debug("Mise en page servie : révision %s, appareil %s", resolved.get("revision"), device_id)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/api/assets/{asset_id}", summary="Image du thème ou d'une tuile")
async def get_asset(asset_id: str, request: Request, user: HaUser = Depends(current_user)) -> Response:
    try:
        path, meta = request.app.state.assets.get(asset_id)
    except AssetError as error:
        raise HTTPException(404, "Ressource introuvable") from error

    etag = f'"{meta["checksum"][:16]}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=meta["contentType"], headers=headers)


@router.websocket("/api/ws")
async def layout_socket(websocket: WebSocket) -> None:
    """Canal de notification : ``layout_updated`` à chaque publication."""
    state = websocket.app.state
    await websocket.accept()

    user = await _authenticate_socket(websocket)
    if user is None:
        await _reject_socket(websocket)
        return

    await websocket.send_json(
        {
            "type": "auth_ok",
            "user": PublicUser(
                id=user.id,
                name=user.name,
                is_admin=user.is_admin,
                is_owner=user.is_owner,
                source=user.source,
            ).model_dump(by_alias=True, mode="json"),
        }
    )
    state.hub.register(websocket)
    logger.info("Application connectée au WebSocket (%d abonnés)", len(state.hub))

    pinger = asyncio.create_task(_ping_loop(websocket))
    try:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("Application déconnectée du WebSocket")
    finally:
        pinger.cancel()
        state.hub.unregister(websocket)


async def _authenticate_socket(websocket: WebSocket) -> HaUser | None:
    state = websocket.app.state
    try:
        first = await asyncio.wait_for(websocket.receive_json(), SOCKET_AUTH_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
        return None

    if not isinstance(first, dict) or first.get("type") != "auth":
        return None
    if is_ingress_request(websocket.scope, state.settings.ingress_port):
        return ingress_user(headers_from_scope(websocket.scope))

    token = first.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    return await authenticate_token(state, token.strip())


async def _reject_socket(websocket: WebSocket) -> None:
    try:
        await websocket.send_json({"type": "auth_error", "detail": "Jeton invalide ou expiré"})
        await websocket.close(code=4401)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _ping_loop(websocket: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(SOCKET_PING_INTERVAL)
            await websocket.send_json({"type": "ping"})
    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
        return


__all__ = ["router"]
