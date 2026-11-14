"""Appairage depuis l'application Android.

Deux routes sans session Home Assistant, accessibles sur le port API (8765) :

* ``POST /api/pair/request`` : l'application annonce sa présence et reçoit un identifiant de
  demande. Le code à six chiffres n'est **pas** dans la réponse : il s'affiche dans le panneau
  Home Assistant (``/internal/pairing``), donc uniquement pour quelqu'un déjà authentifié.
* ``POST /api/pair/claim`` : l'utilisateur recopie ce code ; l'add-on renvoie un jeton
  d'appareil, valable jusqu'à révocation depuis le panneau.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..models.api import PairClaimRequest, PairClaimResponse, PairRequest, PairResponse
from ..store.pairing import CODE_LENGTH, PairingError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pair"])


@router.post(
    "/api/pair/request",
    response_model=PairResponse,
    response_model_by_alias=True,
    summary="Annonce l'application et ouvre une demande d'appairage",
)
async def request_pairing(payload: PairRequest, request: Request) -> PairResponse:
    state = request.app.state
    address = request.client.host if request.client else None

    entry = state.pairing.request(
        device_name=payload.device_name,
        model=payload.model,
        app_version=payload.app_version,
        address=address,
    )
    logger.info(
        "Application en attente du code d'appairage : %s (%s)",
        entry["pairingId"],
        address or "adresse inconnue",
    )
    return PairResponse(
        pairing_id=entry["pairingId"],
        device_id=entry["deviceId"],
        expires_at=entry["expiresAt"],
        expires_in=int(state.pairing.ttl),
        code_length=CODE_LENGTH,
        server_version=state.settings.version,
        ha_uuid=state.ha_uuid,
    )


@router.post(
    "/api/pair/claim",
    response_model=PairClaimResponse,
    response_model_by_alias=True,
    summary="Échange le code affiché dans Home Assistant contre un jeton d'appareil",
)
async def claim_pairing(payload: PairClaimRequest, request: Request) -> PairClaimResponse:
    state = request.app.state
    try:
        result = state.pairing.claim(payload.pairing_id, payload.code)
    except PairingError as error:
        raise HTTPException(error.status, str(error)) from error

    logger.info("Appairage terminé : %s", result["deviceId"])
    return PairClaimResponse(
        token=result["token"],
        device_id=result["deviceId"],
        device_name=result.get("name"),
        server_version=state.settings.version,
        ha_uuid=state.ha_uuid,
    )


__all__ = ["router"]
