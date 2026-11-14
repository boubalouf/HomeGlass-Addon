"""Application FastAPI de HomeGlass Server.

Un seul processus sert l'interface Ingress (8099) et l'API de l'application Android
(``api_port``, 8765 par défaut). Les deux ports partagent le même état : cache Home
Assistant, magasins de données, hub WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from .api.hub import LayoutHub
from .api.ratelimit import RateLimitMiddleware, RateLimiter
from .api.routes_app import router as app_router
from .api.routes_gateway import router as gateway_router
from .api.routes_internal import router as internal_router
from .api.routes_pair import router as pair_router
from .ha.auth import TokenValidator
from .ha.cache import HaCache
from .ha.client import HaWebSocketClient
from .ha.instance import resolve_ha_uuid
from .ingress import IngressPathMiddleware, is_reserved_path
from .models.api import PingResponse
from .settings import Settings
from .store.assets import AssetStore
from .store.devices import DeviceStore
from .store.pairing import PairingStore
from .store.repository import LayoutStore

logger = logging.getLogger(__name__)

_FALLBACK_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>HomeGlass Server</title></head>
<body style="font-family:system-ui;padding:32px">
<h1>HomeGlass Server</h1>
<p>L'interface web n'est pas présente dans cette installation.</p>
</body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()

    cache = HaCache()
    ha_client = HaWebSocketClient(
        settings.core_ws_url,
        settings.supervisor_token,
        on_ready=cache.attach,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # L'identifiant d'instance peut nécessiter un appel HTTP au Supervisor : hors du
        # boucle d'événements pour ne pas la bloquer au démarrage.
        ha_uuid, source = await asyncio.to_thread(resolve_ha_uuid, settings)
        app.state.ha_uuid = ha_uuid
        logger.info("Instance Home Assistant : %s (source : %s)", ha_uuid or "inconnue", source)

        if settings.supervisor_token:
            await ha_client.start()
        else:
            logger.warning(
                "SUPERVISOR_TOKEN absent : aucune connexion à Home Assistant "
                "(mode développement — renseignez HG_CORE_WS_URL pour tester)"
            )

        try:
            yield
        finally:
            await ha_client.stop()

    app = FastAPI(
        title="HomeGlass Server",
        version=settings.version,
        summary="Éditeur et API de mise en page du dashboard HomeGlass",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.ha_uuid = settings.ha_uuid
    app.state.cache = cache
    app.state.ha = ha_client
    app.state.layouts = LayoutStore(settings.data_dir, history_limit=settings.history_limit)
    app.state.devices = DeviceStore(settings.data_dir / "devices.json")
    app.state.pairing = PairingStore(settings.data_dir / "pairing.json")
    app.state.assets = AssetStore(settings.data_dir / "assets", max_bytes=settings.max_asset_bytes)
    app.state.tokens = TokenValidator(settings.auth_ws_url)
    app.state.hub = LayoutHub()

    # Ordre d'exécution : IngressPathMiddleware (normalisation + journal) puis la limite
    # de débit, qui voit donc déjà les chemins corrigés.
    app.add_middleware(
        RateLimitMiddleware,
        limiter=RateLimiter(settings.rate_limit_per_minute, 60.0),
        api_port=settings.api_port,
    )
    app.add_middleware(IngressPathMiddleware)

    app.include_router(app_router)
    app.include_router(pair_router)
    app.include_router(gateway_router)
    app.include_router(internal_router)

    def index_response() -> Response:
        index_file: Path = settings.web_dir / "index.html"
        if not index_file.is_file():
            logger.warning("Page d'accueil absente : %s", index_file)
            return HTMLResponse(_FALLBACK_PAGE)
        return FileResponse(
            index_file,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    async def index() -> Response:
        return index_response()

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    async def favicon() -> Response:
        # Aucune icône dans cette étape : un 204 évite un faux 404 dans la console.
        return Response(status_code=204)

    @app.get(
        "/api/ping",
        response_model=PingResponse,
        response_model_by_alias=True,
        tags=["api"],
        summary="Découverte de l'add-on (sans authentification)",
    )
    async def ping() -> PingResponse:
        return PingResponse(version=settings.version, ha_uuid=app.state.ha_uuid)

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def ingress_fallback(full_path: str) -> Response:
        """Attrape-tout tolérant : tout chemin hors /api, /internal et /assets sert la page.

        Couvre les variantes envoyées par le proxy Ingress (chemin vide, ``//``,
        ``/index.html``, préfixe non retiré) sans jamais masquer une erreur d'API.
        """
        if is_reserved_path(full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        return index_response()

    return app
