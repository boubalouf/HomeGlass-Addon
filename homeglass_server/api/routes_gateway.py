"""Passerelle vers Home Assistant pour l'application appairée.

L'application n'a plus de jeton Home Assistant : elle présente son jeton d'appareil à
l'add-on, qui parle à Core avec ``SUPERVISOR_TOKEN``. Trois points d'entrée suffisent :

* ``GET /api/ha/config`` : version et nom de l'installation ;
* ``POST /api/ha/services/{domain}/{service}`` : appel de service (REST de Core) ;
* ``WS /api/ha/ws`` : relais du protocole WebSocket — l'application y envoie exactement les
  mêmes messages que sur ``/api/websocket`` (``get_states``, ``subscribe_events``, registres,
  ``call_service``), l'add-on se chargeant de l'authentification auprès de Core.

Chaque connexion application ouvre sa propre connexion amont : les identifiants de message
restent ceux du client, sans risque de collision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from ..ha.auth import HaUser
from .deps import authenticate_token, current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])

#: Délai laissé à l'application pour envoyer son message d'authentification.
APP_AUTH_TIMEOUT = 20.0
#: Délai de la poignée de main avec Home Assistant.
UPSTREAM_TIMEOUT = 20.0
#: Délai des appels HTTP vers Core.
HTTP_TIMEOUT = 15.0

_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,50}$")
_CONFIG_KEYS = (
    "version",
    "location_name",
    "time_zone",
    "unit_system",
    "internal_url",
    "external_url",
    "safe_mode",
    "country",
    "language",
)


def _core_call(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as error:
        raise HTTPException(502, f"Home Assistant a répondu {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise HTTPException(502, "Home Assistant injoignable") from error


def _require_supervisor(request: Request) -> str:
    token = request.app.state.settings.supervisor_token
    if not token:
        raise HTTPException(503, "Home Assistant indisponible : jeton Supervisor absent")
    return token


@router.get("/api/ha/config", summary="Configuration de Home Assistant via l'add-on")
async def ha_config(request: Request, user: HaUser = Depends(current_user)) -> dict[str, Any]:
    settings = request.app.state.settings
    token = _require_supervisor(request)
    payload = await asyncio.to_thread(
        _core_call, f"{settings.core_api_url}/config", token
    )
    if not isinstance(payload, dict):
        raise HTTPException(502, "réponse de Home Assistant illisible")
    return {key: payload.get(key) for key in _CONFIG_KEYS if key in payload}


@router.post(
    "/api/ha/services/{domain}/{service}",
    summary="Appel de service Home Assistant via l'add-on",
)
async def ha_service(
    domain: str,
    service: str,
    request: Request,
    user: HaUser = Depends(current_user),
) -> dict[str, Any]:
    if not _DOMAIN_RE.match(domain) or not _DOMAIN_RE.match(service):
        raise HTTPException(422, "domaine ou service invalide")

    settings = request.app.state.settings
    token = _require_supervisor(request)

    body = await request.body()
    payload: dict[str, Any] = {}
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            raise HTTPException(422, "corps JSON invalide") from error
        if not isinstance(parsed, dict):
            raise HTTPException(422, "corps JSON invalide")
        payload = parsed

    result = await asyncio.to_thread(
        _core_call,
        f"{settings.core_api_url}/services/{domain}/{service}",
        token,
        method="POST",
        payload=payload,
    )
    return {"ok": True, "result": result}


@router.websocket("/api/ha/ws")
async def ha_socket(websocket: WebSocket) -> None:
    """Relais authentifié du WebSocket de Home Assistant."""
    state = websocket.app.state
    await websocket.accept()
    await websocket.send_json({"type": "auth_required", "ha_version": state.ha.state.version})

    user, presented = await _authenticate_app(websocket, state)
    if user is None:
        hint = f"…{presented[-4:]}" if presented and len(presented) >= 4 else "absent"
        logger.warning(
            "Passerelle refusée : jeton inconnu (%s), %d appareil(s) appairé(s)",
            hint,
            state.pairing.count(),
        )
        await _reject(websocket)
        return

    settings = state.settings
    if not settings.supervisor_token:
        logger.warning("Passerelle refusée : SUPERVISOR_TOKEN absent")
        await websocket.close(code=1011, reason="Home Assistant indisponible")
        return

    try:
        upstream = await connect(
            settings.core_ws_url,
            open_timeout=UPSTREAM_TIMEOUT,
            close_timeout=2,
            max_size=64 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=20,
        )
    except Exception as error:  # noqa: BLE001 - toute panne amont ferme la passerelle
        logger.warning("Passerelle : Home Assistant injoignable (%s)", error)
        await websocket.close(code=1011, reason="Home Assistant injoignable")
        return

    try:
        hello = json.loads(await asyncio.wait_for(upstream.recv(), UPSTREAM_TIMEOUT))
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"premier message inattendu : {hello.get('type')!r}")

        await upstream.send(json.dumps({"type": "auth", "access_token": settings.supervisor_token}))
        reply = json.loads(await asyncio.wait_for(upstream.recv(), UPSTREAM_TIMEOUT))
        if reply.get("type") != "auth_ok":
            raise RuntimeError("authentification Supervisor refusée par Home Assistant")

        await websocket.send_json({"type": "auth_ok", "ha_version": reply.get("ha_version")})
        logger.info("Passerelle Home Assistant ouverte pour %s", user.id)
        await _relay(websocket, upstream)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as error:  # noqa: BLE001 - on ferme proprement côté application
        logger.warning("Passerelle Home Assistant interrompue : %s", error)
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 - fermeture best effort
            pass


async def _authenticate_app(websocket: WebSocket, state: Any) -> tuple[HaUser | None, str | None]:
    try:
        first = await asyncio.wait_for(websocket.receive_json(), APP_AUTH_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
        return None, None

    if not isinstance(first, dict) or first.get("type") != "auth":
        return None, None
    # Home Assistant nomme le champ ``access_token`` ; on tolère aussi ``token``.
    token = first.get("access_token") or first.get("token")
    if not isinstance(token, str) or not token.strip():
        return None, None
    presented = token.strip()
    return await authenticate_token(state, presented), presented


async def _reject(websocket: WebSocket) -> None:
    logger.warning("Passerelle refusée : jeton d'appareil invalide ou expiré")
    try:
        await websocket.send_json({"type": "auth_invalid", "message": "Jeton invalide ou expiré"})
        await websocket.close(code=4401)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _relay(websocket: WebSocket, upstream: Any) -> None:
    """Fait circuler les messages dans les deux sens jusqu'à la première fermeture."""
    tasks = [
        asyncio.create_task(_client_to_core(websocket, upstream)),
        asyncio.create_task(_core_to_client(websocket, upstream)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _client_to_core(websocket: WebSocket, upstream: Any) -> None:
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        kind = message.get("type")
        # L'authentification est terminée : aucun message d'auth ne doit remonter à Core.
        if kind in {"auth", "auth_required", "auth_ok", "auth_invalid"}:
            continue
        if kind == "ping":
            await websocket.send_json({"type": "pong"})
            continue
        await upstream.send(raw)


async def _core_to_client(websocket: WebSocket, upstream: Any) -> None:
    async for raw in upstream:
        await websocket.send_text(raw if isinstance(raw, str) else raw.decode("utf-8"))


__all__ = ["router"]
