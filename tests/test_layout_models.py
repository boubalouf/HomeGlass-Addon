from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from homeglass_server.models import (
    SCHEMA_VERSION,
    Layout,
    SceneSection,
    TileSection,
)


def assert_contains(actual: object, expected: object, path: str = "document") -> None:
    """Vérifie que ``actual`` contient toutes les valeurs de ``expected``.

    L'API produit toujours l'intégralité des champs : le document sérialisé peut donc
    contenir des clés optionnelles supplémentaires (à ``null``), mais aucune valeur de
    l'exemple de référence ne doit être perdue ni modifiée.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path} devrait être un objet"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} absent du document sérialisé"
            assert_contains(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path} devrait être une liste"
        assert len(actual) == len(expected), f"{path} : longueur inattendue"
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            assert_contains(actual_item, expected_item, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path} : {actual!r} != {expected!r}"


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


def test_example_layout_is_accepted_and_preserved(example_payload: dict) -> None:
    layout = Layout.model_validate(example_payload)
    dumped = layout.model_dump(by_alias=True, mode="json")

    assert_contains(dumped, example_payload)
    assert dumped["revision"] == 42
    assert dumped["publishedAt"] == "2026-11-04T13:00:00Z"


def test_sections_are_typed_by_kind(example_payload: dict) -> None:
    home = Layout.model_validate(example_payload).home

    assert isinstance(home.sections[0], SceneSection)
    assert isinstance(home.sections[1], TileSection)
    assert home.sections[0].items[0].gradient == ["#FF9500", "#FF375F"]


def test_all_fields_are_always_serialized() -> None:
    dumped = Layout(published_at="2026-11-04T13:00:00Z").model_dump(by_alias=True, mode="json")

    assert set(dumped) == {
        "schemaVersion",
        "revision",
        "publishedAt",
        "theme",
        "tabs",
        "home",
        "rooms",
    }
    assert set(dumped["theme"]) == {"mode", "accent", "tileRadiusDp", "density", "background"}
    assert set(dumped["theme"]["background"]) == {"type", "assetId", "blurDp", "dim"}
    assert set(dumped["tabs"][0]) == {"id", "type", "label", "icon", "enabled"}
    assert dumped["home"]["summaryChips"] == ["lights", "climate", "security", "media"]
    assert len(dumped["tabs"]) == 4


def test_published_at_is_normalized_to_utc() -> None:
    layout = Layout(published_at="2026-11-04T14:00:00+01:00")

    dumped = layout.model_dump(by_alias=True, mode="json")

    assert dumped["publishedAt"] == "2026-11-04T13:00:00Z"


def test_defaults_match_the_design_system() -> None:
    theme = Layout(published_at="2026-11-04T13:00:00Z").theme

    assert theme.accent == "#FF9500"
    assert theme.tile_radius_dp == 22
    assert theme.mode.value == "auto"
    assert theme.density.value == "standard"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda p: p["rooms"][0].update({"areaId": "Salon!"}), "areaId non slug"),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"entityId": "Lampe du salon"}),
            "entityId hors format Home Assistant",
        ),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"accent": "orange"}),
            "couleur non hexadécimale",
        ),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"icon": "Light Bulb"}),
            "icône non snake_case",
        ),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"tap": {"type": "call_service"}}),
            "call_service sans service",
        ),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"longPress": {"type": "navigate"}}),
            "navigate sans roomId",
        ),
        (
            lambda p: p["home"]["sections"][1]["items"][0].update({"unknownField": True}),
            "champ inconnu",
        ),
        (lambda p: p.update({"schemaVersion": 2}), "schemaVersion inconnu"),
        (
            lambda p: p["home"]["sections"][0]["items"][0].update({"gradient": ["#FF9500"]}),
            "dégradé de scène incomplet",
        ),
        (
            lambda p: p["theme"]["background"].update({"type": "image"}),
            "fond image sans assetId",
        ),
    ],
)
def test_contract_violations_are_rejected(
    example_payload: dict,
    mutation,
    reason: str,
) -> None:
    payload = copy.deepcopy(example_payload)
    mutation(payload)

    with pytest.raises(ValidationError):
        Layout.model_validate(payload)
    assert reason  # documente le cas testé


def test_scene_actions_require_valid_services(example_payload: dict) -> None:
    payload = copy.deepcopy(example_payload)
    payload["home"]["sections"][0]["items"][0]["actions"] = [{"type": "call_service", "service": "scene"}]

    with pytest.raises(ValidationError):
        Layout.model_validate(payload)
