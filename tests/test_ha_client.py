from __future__ import annotations

import asyncio

from fake_ha import wait_until
from homeglass_server.ha.auth import TokenValidator
from homeglass_server.ha.cache import HaCache
from homeglass_server.ha.client import HaWebSocketClient


def build_client(fake, cache: HaCache | None = None, **kwargs) -> tuple[HaWebSocketClient, HaCache]:
    cache = cache or HaCache()
    client = HaWebSocketClient(
        fake.supervisor_url,
        fake.supervisor_token,
        on_ready=cache.attach,
        min_backoff=0.05,
        max_backoff=0.2,
        **kwargs,
    )
    return client, cache


def test_client_authenticates_and_fills_the_cache(fake_ha) -> None:
    async def scenario() -> None:
        client, cache = build_client(fake_ha)
        await client.start()
        await wait_until(lambda: len(cache.entities) == len(fake_ha.entities))

        assert client.state.connected is True
        assert client.state.version == "2026.11.0"
        assert client.state.last_error is None
        assert fake_ha.supervisor_connections == 1
        assert cache.location_name == "Maison test"

        # L'entité n'a pas de pièce : elle hérite de celle de son appareil.
        assert cache.area_for(cache.entities["light.salon"]) == "salon"
        assert cache.area_for(cache.entities["switch.bureau"]) is None
        assert "get_states" in fake_ha.command_types()
        # Les abonnements sont posés juste après le chargement initial : on les attend.
        await wait_until(fake_ha.ready)
        assert fake_ha.ready() is True

        await client.stop()

    asyncio.run(scenario())


def test_client_refuses_the_connection_with_an_invalid_token(fake_ha) -> None:
    async def scenario() -> None:
        client = HaWebSocketClient(
            fake_ha.supervisor_url,
            "mauvais-jeton",
            min_backoff=0.05,
            max_backoff=0.1,
        )
        await client.start()
        await wait_until(lambda: "refusé" in (client.state.last_error or ""))
        assert client.state.connected is False
        await client.stop()

    asyncio.run(scenario())


def test_client_reconnects_and_reloads_the_cache(fake_ha) -> None:
    async def scenario() -> None:
        client, cache = build_client(fake_ha)
        await client.start()
        await wait_until(lambda: len(cache.entities) == len(fake_ha.entities))
        assert fake_ha.supervisor_connections == 1

        await fake_ha.drop_connections()

        await wait_until(lambda: fake_ha.supervisor_connections >= 2)
        await wait_until(lambda: client.state.connected and fake_ha.ready())
        assert client.state.attempts == 0
        # Les registres sont rechargés après reconnexion.
        assert fake_ha.command_types().count("get_states") >= 2

        await client.stop()

    asyncio.run(scenario())


def test_state_changed_updates_the_cache(fake_ha) -> None:
    async def scenario() -> None:
        client, cache = build_client(fake_ha)
        await client.start()
        await wait_until(lambda: fake_ha.ready())
        before = cache.revision

        await fake_ha.fire(
            "state_changed",
            {
                "entity_id": "light.salon",
                "new_state": {
                    "entity_id": "light.salon",
                    "state": "off",
                    "attributes": {"friendly_name": "Lampe salon", "brightness": None},
                },
            },
        )

        await wait_until(lambda: cache.entities["light.salon"].state == "off")
        assert cache.revision > before
        assert "brightness" in cache.entities["light.salon"].attributes
        await client.stop()

    asyncio.run(scenario())


def test_registry_event_triggers_a_full_refresh(fake_ha) -> None:
    async def scenario() -> None:
        client, cache = build_client(fake_ha)
        await client.start()
        await wait_until(lambda: fake_ha.ready())
        assert "light.couloir" not in cache.entities

        fake_ha.entities.append(
            {
                "entity_id": "light.couloir",
                "original_name": "Couloir",
                "name": None,
                "area_id": "salon",
                "device_id": None,
                "icon": None,
                "entity_category": None,
                "disabled_by": None,
                "hidden_by": None,
                "platform": "zha",
            }
        )
        await fake_ha.fire(
            "entity_registry_updated", {"action": "create", "entity_id": "light.couloir"}
        )

        await wait_until(lambda: "light.couloir" in cache.entities)
        await client.stop()

    asyncio.run(scenario())


def test_token_validator_accepts_and_caches_a_valid_token(fake_ha) -> None:
    async def scenario() -> None:
        validator = TokenValidator(fake_ha.auth_url)
        user = await validator.validate("good-token")

        assert user is not None
        assert user.is_admin is True
        assert user.is_owner is True
        assert user.name == "Timofei"

        # Deuxième appel : servi par le cache (aucune nouvelle connexion).
        assert await validator.validate("good-token") == user
        assert fake_ha.auth_connections == 1

    asyncio.run(scenario())


def test_token_validator_rejects_invalid_and_non_admin_tokens(fake_ha) -> None:
    async def scenario() -> None:
        validator = TokenValidator(fake_ha.auth_url)
        assert await validator.validate("inconnu") is None

        user = await validator.validate("user-token")
        assert user is not None
        assert user.is_admin is False

    asyncio.run(scenario())


def test_token_validator_reports_an_unreachable_home_assistant() -> None:
    async def scenario() -> None:
        validator = TokenValidator("ws://127.0.0.1:9/api/websocket", timeout=1.0)
        assert await validator.validate("peu-importe") is None

    asyncio.run(scenario())
