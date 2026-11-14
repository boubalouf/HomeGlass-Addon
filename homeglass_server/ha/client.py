"""Client WebSocket vers Home Assistant Core.

L'add-on parle à Core via le proxy du Supervisor (``ws://supervisor/core/websocket``),
authentifié avec ``SUPERVISOR_TOKEN``. Le client :

* s'authentifie selon le protocole ``auth_required`` → ``auth`` → ``auth_ok`` ;
* route les réponses (``result``) vers les futures en attente et les événements
  (``event``) vers les abonnements enregistrés ;
* se reconnecte indéfiniment avec un délai exponentiel (1 s → 60 s), remis à zéro dès
  qu'une session a été authentifiée ;
* expose son état (connecté, tentative en cours, dernière erreur) pour
  ``GET /internal/status``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0
COMMAND_TIMEOUT = 20.0
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0

EventHandler = Callable[[dict[str, Any]], Any]


class HaError(Exception):
    """Erreur renvoyée par Home Assistant ou par le transport WebSocket."""


class HaAuthError(HaError):
    """Home Assistant a refusé le jeton."""


class HaNotConnected(HaError):
    """Commande envoyée alors qu'aucune connexion n'est établie."""


@dataclass
class HaConnectionState:
    """État observable de la connexion, exposé par l'API interne."""

    connected: bool = False
    attempts: int = 0
    last_error: str | None = None
    connected_since: datetime | None = None
    version: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "attempts": self.attempts,
            "lastError": self.last_error,
            "connectedSince": (
                self.connected_since.astimezone(timezone.utc).isoformat()
                if self.connected_since
                else None
            ),
            "version": self.version,
        }


def describe_connection_error(error: BaseException) -> str:
    """Message lisible pour les erreurs de connexion les plus courantes."""
    if isinstance(error, ConnectionRefusedError):
        return (
            "connexion refusée : Home Assistant Core n'est pas joignable "
            "(démarrage en cours, ou URL invalide hors add-on)"
        )
    if isinstance(error, socket.gaierror):
        return (
            "nom d'hôte introuvable : hors add-on, renseignez HG_CORE_WS_URL "
            "(par exemple ws://homeassistant.local:8123/api/websocket)"
        )
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "délai d'attente dépassé pendant la poignée de main WebSocket"
    if isinstance(error, OSError):
        return f"erreur réseau : {error}"
    return f"{type(error).__name__} : {error}"


async def _consume(awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - un gestionnaire fautif ne doit pas tuer la lecture
        logger.exception("Gestionnaire d'événement Home Assistant en échec")


class HaWebSocketClient:
    """Connexion unique et persistante à l'API WebSocket de Home Assistant."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        on_ready: Callable[["HaWebSocketClient"], Any] | None = None,
        min_backoff: float = MIN_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
        command_timeout: float = COMMAND_TIMEOUT,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.token = token
        self.state = HaConnectionState()
        self.on_ready = on_ready
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._command_timeout = command_timeout
        self._connect_factory = connect_factory or connect

        self._ws: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._authed = False
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, EventHandler] = {}
        self._next_id = 1

    # ------------------------------------------------------------------ cycle de vie

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Démarre la boucle de connexion (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="homeglass-ha-ws")

    async def stop(self) -> None:
        """Arrête proprement la boucle et ferme la connexion."""
        self._stopping = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - fermeture best effort
                logger.debug("Fermeture du WebSocket Home Assistant déjà close")
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._cleanup_session()

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self._authed = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except HaAuthError as error:
                self.state.last_error = f"authentification refusée par Home Assistant : {error}"
                logger.error(
                    "Home Assistant a refusé le jeton Supervisor : %s "
                    "(SUPERVISOR_TOKEN absent, expiré, ou add-on sans homeassistant_api)",
                    error,
                )
            except Exception as error:  # noqa: BLE001 - toute panne réseau est retentée
                self.state.last_error = describe_connection_error(error)
                logger.warning("Connexion à Home Assistant interrompue : %s", self.state.last_error)
            finally:
                self._cleanup_session()

            if self._stopping:
                break

            # Une session authentifiée qui tombe repart vite ; une connexion impossible
            # attend de plus en plus longtemps (1, 2, 4 … 60 s).
            attempt = 0 if self._authed else attempt + 1
            delay = min(self._max_backoff, self._min_backoff * (2**attempt))
            self.state.attempts = attempt
            logger.info(
                "Nouvelle tentative de connexion à Home Assistant dans %.0f s (tentative %d)",
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        """Ouvre une session, s'authentifie, puis lit les messages jusqu'à la fermeture."""
        async with self._connect_factory(
            self.url,
            open_timeout=self._command_timeout,
            close_timeout=2,
            max_size=64 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            hello = json.loads(await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT))
            if hello.get("type") != "auth_required":
                raise HaAuthError(f"premier message inattendu : {hello.get('type')!r}")

            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT))
            if reply.get("type") == "auth_invalid":
                raise HaAuthError(reply.get("message") or "jeton refusé")
            if reply.get("type") != "auth_ok":
                raise HaAuthError(f"réponse d'authentification inattendue : {reply.get('type')!r}")

            self._authed = True
            self.state.connected = True
            self.state.attempts = 0
            self.state.last_error = None
            self.state.connected_since = datetime.now(timezone.utc)
            self.state.version = reply.get("ha_version")
            logger.info(
                "Connecté à Home Assistant %s via %s",
                reply.get("ha_version") or "?",
                self.url,
            )

            # La lecture démarre avant ``on_ready`` : celui-ci envoie des commandes
            # (registres, états) dont les résultats n'arrivent que par cette boucle.
            reader = asyncio.create_task(self._read_loop(ws))
            ready_task: asyncio.Task[None] | None = None
            try:
                if self.on_ready is not None:
                    ready_task = asyncio.create_task(self._run_on_ready())
                    # Si la connexion tombe pendant le chargement initial, les commandes en
                    # attente ne recevront jamais de réponse : on abandonne la session tout de
                    # suite (retentée après le backoff) au lieu d'attendre leur expiration.
                    done, _ = await asyncio.wait(
                        {reader, ready_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if ready_task in done:
                        ready_task.result()
                    else:
                        raise HaError(
                            "connexion Home Assistant fermée pendant le chargement initial"
                        )
                await reader
            finally:
                if ready_task is not None and not ready_task.done():
                    ready_task.cancel()
                    try:
                        await ready_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                reader.cancel()
                try:
                    await reader
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _run_on_ready(self) -> None:
        """Exécute le rappel de chargement initial (synchrone ou asynchrone)."""
        if self.on_ready is None:
            return
        ready = self.on_ready(self)
        if inspect.isawaitable(ready):
            await ready

    async def _read_loop(self, ws: Any) -> None:
        """Lit les messages jusqu'à la fermeture et les distribue."""
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Message Home Assistant illisible : %r", str(raw)[:200])
                continue
            if isinstance(message, dict):
                self._dispatch(message)

    def _cleanup_session(self) -> None:
        self.state.connected = False
        self._ws = None
        self._subscriptions.clear()
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(HaNotConnected("connexion Home Assistant fermée"))
        for future in pending.values():
            # Évite « Future exception was never retrieved » si personne n'attend plus.
            future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    # ------------------------------------------------------------------- messages

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "result":
            future = self._pending.pop(message.get("id"), None)
            if future is None or future.done():
                return
            if message.get("success"):
                future.set_result(message.get("result"))
            else:
                error = message.get("error") or {}
                future.set_exception(
                    HaError(f"{error.get('code') or 'erreur'} : {error.get('message') or ''}".strip())
                )
            return

        if kind == "event":
            handler = self._subscriptions.get(message.get("id"))
            if handler is None:
                return
            event = message.get("event") or {}
            try:
                result = handler(event)
            except Exception:  # noqa: BLE001 - un abonné fautif ne coupe pas le flux
                logger.exception("Traitement d'événement Home Assistant en échec")
                return
            if inspect.isawaitable(result):
                asyncio.create_task(_consume(result))

    async def _request(self, type_: str, **payload: Any) -> tuple[int, Any]:
        """Envoie une commande et attend son résultat, en gardant l'identifiant."""
        ws = self._ws
        if ws is None or not self.state.connected:
            raise HaNotConnected("Home Assistant n'est pas connecté")

        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await ws.send(json.dumps({"id": message_id, "type": type_, **payload}))
        try:
            result = await asyncio.wait_for(future, self._command_timeout)
        except asyncio.TimeoutError as error:
            raise HaError(f"commande « {type_} » expirée") from error
        finally:
            self._pending.pop(message_id, None)
        return message_id, result

    async def command(self, type_: str, **payload: Any) -> Any:
        """Exécute une commande et renvoie son résultat."""
        _, result = await self._request(type_, **payload)
        return result

    async def subscribe_events(self, event_type: str, handler: EventHandler) -> None:
        """Abonne ``handler`` aux événements ``event_type`` de Core.

        L'abonné est enregistré *avant* l'envoi de la commande : un événement peut arriver
        juste après la réponse de Core, et serait perdu s'il n'y avait pas encore de
        gestionnaire pour cet identifiant.
        """
        ws = self._ws
        if ws is None or not self.state.connected:
            raise HaNotConnected("Home Assistant n'est pas connecté")

        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        self._subscriptions[message_id] = handler
        kept = False
        try:
            await ws.send(
                json.dumps(
                    {"id": message_id, "type": "subscribe_events", "event_type": event_type}
                )
            )
            await asyncio.wait_for(future, self._command_timeout)
            kept = True
        except asyncio.TimeoutError as error:
            raise HaError("commande « subscribe_events » expirée") from error
        finally:
            self._pending.pop(message_id, None)
            if not kept:
                self._subscriptions.pop(message_id, None)

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> Any:
        """Appelle un service Home Assistant (utilisé par l'éditeur)."""
        return await self.command(
            "call_service",
            domain=domain,
            service=service,
            service_data=service_data or {},
            target=target or {},
        )
