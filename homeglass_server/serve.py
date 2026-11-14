"""Point d'entrée du serveur.

Un seul processus uvicorn écoute deux sockets : le port Ingress (8099) et, si l'option
``api_port`` est renseignée, le port de l'API pour l'application Android (8765 par défaut).
Un seul gestionnaire de signaux, donc un arrêt propre de l'add-on.
"""

from __future__ import annotations

import logging
import socket

import uvicorn

from .app import create_app
from .ingress import ObservingH11Protocol
from .settings import INGRESS_PORT, Settings

#: Niveaux d'uvicorn : les valeurs Home Assistant « notice »/« fatal » n'y existent pas.
_UVICORN_LEVELS = {"notice": "info", "fatal": "critical"}

logger = logging.getLogger(__name__)


def _bind(port: int) -> socket.socket | None:
    """Ouvre un socket d'écoute ; renvoie None si le port est déjà occupé."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as error:
        sock.close()
        logger.warning("Port %d indisponible (%s) : écoute ignorée", port, error)
        return None
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def listening_ports(settings: Settings) -> list[int]:
    """Ports à ouvrir : Ingress d'abord, puis l'API pour l'application Android."""
    ingress = settings.ingress_port or INGRESS_PORT
    ports = [ingress]
    if settings.api_port and settings.api_port != ingress:
        ports.append(settings.api_port)
    return ports


def build_config(app: object, settings: Settings) -> uvicorn.Config:
    """Configuration uvicorn commune aux deux sockets.

    ``log_config=None`` : la journalisation est entièrement gérée par
    [Settings.configure_logging] (un seul format, et un handler sur la racine qui rend
    ``hasHandlers()`` vrai — condition pour qu'uvicorn émette ses lignes d'accès).
    """
    return uvicorn.Config(
        app,
        http=ObservingH11Protocol,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        access_log=True,
        log_config=None,
        log_level=_UVICORN_LEVELS.get(settings.log_level, settings.log_level),
    )


def main() -> None:
    settings = Settings.load()
    settings.configure_logging()

    app = create_app(settings)
    ports = listening_ports(settings)
    sockets = [sock for port in ports if (sock := _bind(port)) is not None]
    if not sockets:
        raise SystemExit("Aucun port d'écoute disponible.")

    config = build_config(app, settings)

    logger.info(
        "HomeGlass Server %s démarré — écoute sur : %s (Ingress : %d, API Android : %s)",
        settings.version,
        ", ".join(str(port) for port in ports),
        settings.ingress_port or INGRESS_PORT,
        settings.api_port if settings.api_port in ports else "désactivée",
    )
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    main()
