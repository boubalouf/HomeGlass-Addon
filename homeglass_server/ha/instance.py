"""Identifiant unique de l'instance Home Assistant (``ha_uuid``).

``ha_uuid`` sert à l'application Android (et au futur TXT mDNS) à reconnaître *une*
installation. Home Assistant n'expose pas son UUID d'instance (celui de
``homeassistant.helpers.instance_id``) dans ``GET /api/config`` ; on l'espère donc dans
l'une des réponses que le Supervisor sait fournir, et sinon on persiste un identifiant
local stable dans le dossier de données.

Ordre de résolution, journalisé au démarrage :

1. ``HG_HA_UUID`` (configuration explicite, pratique en développement) ;
2. ``GET {core_api_url}/config`` (proxy Core par le Supervisor) — clés ``uuid`` ou
   ``instance_id`` si une version de Core les expose ;
3. ``GET http://supervisor/supervisor/info`` — clé ``uuid`` ou ``supervisor.uuid`` ;
4. identifiant local persisté dans ``{data_dir}/ha_uuid`` (UUID v4, généré une fois).

Le résultat est toujours stable d'un démarrage à l'autre.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ..settings import Settings

logger = logging.getLogger(__name__)

_UUID_FILENAME = "ha_uuid"
_HTTP_TIMEOUT = 3.0


def resolve_ha_uuid(settings: Settings) -> tuple[str, str]:
    """Renvoie ``(uuid, source)`` ; la source est destinée au journal de démarrage."""
    if settings.ha_uuid:
        return settings.ha_uuid, "configuration (HG_HA_UUID)"

    if settings.supervisor_token:
        for url, path in (
            (f"{settings.core_api_url}/config", ("uuid",)),
            (f"{settings.core_api_url}/config", ("instance_id",)),
            ("http://supervisor/supervisor/info", ("uuid",)),
            ("http://supervisor/supervisor/info", ("supervisor", "uuid")),
        ):
            value = _dig(_get_json(url, settings.supervisor_token), path)
            if isinstance(value, str) and value.strip():
                return value.strip(), f"Home Assistant ({url})"

    value = _persisted_uuid(settings.data_dir)
    return value, f"identifiant local persistant ({settings.data_dir / _UUID_FILENAME})"


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _get_json(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as error:
        logger.debug("Identifiant d'instance : %s injoignable (%s)", url, error)
        return None


def _persisted_uuid(data_dir: Path) -> str:
    path = Path(data_dir) / _UUID_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing

    generated = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated + "\n", encoding="utf-8")
    except OSError as error:
        logger.warning("Identifiant d'instance non persistable (%s) : valeur non stable", error)
    return generated


__all__ = ["resolve_ha_uuid"]
