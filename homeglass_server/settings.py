"""Réglages de l'add-on, lus depuis les options Home Assistant et les variables d'environnement.

Home Assistant écrit les options dans ``/data/options.json``. ``run.sh`` les recopie dans
des variables d'environnement (via bashio) pour que le serveur fonctionne aussi hors
add-on, en développement.

Les réglages qui ne sont pas des options d'add-on (jeton Supervisor, URL de Core) sont lus
uniquement dans l'environnement : ce sont des valeurs fournies par le Supervisor lui-même.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import __version__

logger = logging.getLogger(__name__)

#: Port d'écoute derrière Ingress (doit correspondre à ``ingress_port`` de config.yaml).
INGRESS_PORT = 8099
DEFAULT_API_PORT = 8765
DEFAULT_LOG_LEVEL = "info"
LOG_LEVELS = {"trace", "debug", "info", "notice", "warning", "error", "fatal"}

#: Proxy WebSocket de Home Assistant Core exposé par le Supervisor.
DEFAULT_CORE_WS_URL = "ws://supervisor/core/websocket"
#: API HTTP de Core (proxy Supervisor), utilisée pour l'identifiant d'instance.
DEFAULT_CORE_API_URL = "http://supervisor/core/api"
#: API WebSocket de Core via le réseau interne des add-ons (validation des jetons).
DEFAULT_AUTH_WS_URL = "ws://homeassistant:8123/api/websocket"

DEFAULT_HISTORY_LIMIT = 20
DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024
DEFAULT_RATE_LIMIT = 120

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_WEB_DIR = _PACKAGE_ROOT.parent / "web"
_DEFAULT_SCHEMA_DIR = _PACKAGE_ROOT.parent / "schema"


@dataclass(frozen=True, slots=True)
class Settings:
    log_level: str = DEFAULT_LOG_LEVEL
    api_port: int | None = DEFAULT_API_PORT
    allow_remote_access: bool = False
    data_dir: Path = Path("/data")
    web_dir: Path = _DEFAULT_WEB_DIR
    schema_dir: Path = _DEFAULT_SCHEMA_DIR
    ha_uuid: str = ""
    version: str = __version__

    #: Jeton du Supervisor, seul moyen de s'authentifier auprès de Core.
    supervisor_token: str = ""
    core_ws_url: str = DEFAULT_CORE_WS_URL
    core_api_url: str = DEFAULT_CORE_API_URL
    auth_ws_url: str = DEFAULT_AUTH_WS_URL

    #: Port Ingress : seul port depuis lequel les routes ``/internal`` sont accessibles.
    ingress_port: int = INGRESS_PORT
    history_limit: int = DEFAULT_HISTORY_LIMIT
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT

    @classmethod
    def load(
        cls,
        options_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        env = environ if environ is not None else os.environ

        options: dict[str, Any] = {}
        path = options_path or Path(env.get("HG_OPTIONS_PATH", "/data/options.json"))
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    options = loaded
            except (OSError, json.JSONDecodeError) as error:
                logger.warning("Options illisibles (%s) : valeurs par défaut utilisées", error)

        log_level = str(
            env.get("HG_LOG_LEVEL") or options.get("log_level") or DEFAULT_LOG_LEVEL
        ).strip().lower()
        if log_level not in LOG_LEVELS:
            logger.warning("Niveau de journalisation inconnu %r : 'info' utilisé", log_level)
            log_level = DEFAULT_LOG_LEVEL

        api_port: int | None
        try:
            api_port = _coerce_port(
                env.get("HG_API_PORT", options.get("api_port", DEFAULT_API_PORT))
            )
        except (TypeError, ValueError) as error:
            logger.warning("api_port invalide (%s) : API désactivée", error)
            api_port = None

        return cls(
            log_level=log_level,
            api_port=api_port,
            allow_remote_access=_coerce_bool(
                env.get("HG_ALLOW_REMOTE_ACCESS", options.get("allow_remote_access", False))
            ),
            data_dir=Path(env.get("HG_DATA_DIR", "/data")),
            web_dir=Path(env.get("HG_WEB_DIR", str(_DEFAULT_WEB_DIR))),
            schema_dir=Path(env.get("HG_SCHEMA_DIR", str(_DEFAULT_SCHEMA_DIR))),
            ha_uuid=(env.get("HG_HA_UUID") or "").strip(),
            supervisor_token=(env.get("SUPERVISOR_TOKEN") or "").strip(),
            core_ws_url=(env.get("HG_CORE_WS_URL") or DEFAULT_CORE_WS_URL).strip(),
            core_api_url=(env.get("HG_CORE_API_URL") or DEFAULT_CORE_API_URL).rstrip("/"),
            auth_ws_url=(env.get("HG_AUTH_WS_URL") or DEFAULT_AUTH_WS_URL).strip(),
            ingress_port=_coerce_int(env.get("HG_INGRESS_PORT"), INGRESS_PORT),
            history_limit=max(1, _coerce_int(env.get("HG_HISTORY_LIMIT"), DEFAULT_HISTORY_LIMIT)),
            max_asset_bytes=max(
                1024, _coerce_int(env.get("HG_MAX_ASSET_BYTES"), DEFAULT_MAX_ASSET_BYTES)
            ),
            rate_limit_per_minute=max(
                0, _coerce_int(env.get("HG_RATE_LIMIT"), DEFAULT_RATE_LIMIT)
            ),
        )

    def configure_logging(self) -> None:
        level = {
            "trace": logging.DEBUG,
            "notice": logging.INFO,
            "fatal": logging.CRITICAL,
        }.get(self.log_level, getattr(logging, self.log_level.upper(), logging.INFO))
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        )

        # uvicorn journalise via « uvicorn.error » et « uvicorn.access ». On les fait remonter
        # au gestionnaire racine (un seul format) et on force le niveau INFO du journal
        # d'accès : uvicorn n'émet ses lignes d'accès que si ce logger a un handler.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers.clear()
            uvicorn_logger.propagate = True
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)


def _coerce_port(value: object) -> int | None:
    """Convertit une option de port ; toute valeur vide désactive l'API."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("booléen reçu à la place d'un numéro de port")
    if isinstance(value, int):
        port = value
    else:
        text = str(value).strip()
        if not text:
            return None
        port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port hors bornes : {port}")
    return port


def _coerce_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("Valeur entière invalide %r : %d utilisé", value, default)
        return default


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
