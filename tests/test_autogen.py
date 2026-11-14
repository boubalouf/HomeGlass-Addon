from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from fake_ha import build_cache
from homeglass_server.ha.autogen import exclusion_reason, generate_layout
from homeglass_server.models import Layout

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schema" / "layout.v1.json").read_text(encoding="utf-8")
)


def generate() -> dict:
    return generate_layout(build_cache()).model_dump(by_alias=True, mode="json")


def section_ids(document: dict) -> list[str]:
    return [section["id"] for section in document["home"]["sections"]]


def sections_of(room: dict) -> dict[str, list[str]]:
    return {
        section["id"]: [item["entityId"] for item in section["items"]]
        for section in room["sections"]
    }


def test_generated_layout_is_valid_against_the_published_schema() -> None:
    document = generate()

    jsonschema.validate(document, SCHEMA)
    assert document["schemaVersion"] == 1
    assert document["revision"] == 1
    # Le cache et le modèle produisent un document complet, champs null compris.
    assert document["theme"]["background"]["assetId"] is None
    assert Layout.model_validate(document)


def test_rooms_follow_areas_and_unassigned_entities_get_their_own_room() -> None:
    document = generate()
    rooms = {room["id"]: room for room in document["rooms"]}

    assert [room["id"] for room in document["rooms"]] == ["room_cuisine", "room_salon", "room_autres"]
    assert rooms["room_salon"]["name"] == "Salon"
    assert rooms["room_salon"]["areaId"] == "salon"
    assert rooms["room_autres"]["areaId"] is None
    assert rooms["room_autres"]["icon"] == "devices_other"


def test_sections_group_domains_per_room() -> None:
    document = generate()
    salon = next(room for room in document["rooms"] if room["id"] == "room_salon")
    sections = sections_of(salon)

    assert list(sections) == ["sec_room_salon_lights", "sec_room_salon_security", "sec_room_salon_sensors"]
    assert sections["sec_room_salon_lights"] == ["light.salon"]
    assert sections["sec_room_salon_security"] == ["lock.entree"]
    assert sections["sec_room_salon_sensors"] == ["sensor.salon_temperature"]


def test_technical_entities_are_excluded() -> None:
    document = generate()
    serialized = json.dumps(document)

    assert "sensor.salon_wifi_signal" not in serialized  # catégorie diagnostic
    assert "binary_sensor.salon_mouvement" not in serialized  # désactivée
    assert "automation.reveil" not in serialized  # domaine technique

    cache = build_cache()
    assert exclusion_reason(cache.entities["sensor.salon_wifi_signal"]) == "catégorie diagnostic"
    assert exclusion_reason(cache.entities["light.salon"]) is None


def test_scenes_land_on_the_home_tab_with_distinct_gradients() -> None:
    document = generate()
    scene_section = document["home"]["sections"][0]

    assert scene_section["id"] == "sec_scenes"
    assert scene_section["kind"] == "scenes"
    assert [item["name"] for item in scene_section["items"]] == ["Réveil", "Soirée"]
    gradients = [tuple(item["gradient"]) for item in scene_section["items"]]
    assert len(set(gradients)) == 2
    assert scene_section["items"][1]["actions"] == [
        {
            "type": "call_service",
            "service": "scene.turn_on",
            "data": None,
            "target": {"entity_id": "scene.soiree"},
            "roomId": None,
        }
    ]


def test_home_always_contains_an_empty_favourites_section() -> None:
    document = generate()

    assert "sec_scenes" in section_ids(document)
    favourites = document["home"]["sections"][-1]
    assert favourites["id"] == "sec_favoris"
    assert favourites["kind"] == "tiles"
    assert favourites["title"] == "Favoris"
    assert favourites["items"] == []
    assert document["home"]["title"] == "Maison test"


def test_display_mode_and_size_adapt_to_the_domain() -> None:
    document = generate()
    tiles = {
        section_item["entityId"]: section_item
        for room in document["rooms"]
        for section in room["sections"]
        for section_item in section["items"]
    }

    assert tiles["light.salon"]["display"] == "slider"
    assert tiles["light.salon"]["tap"]["type"] == "toggle"
    assert tiles["light.cuisine_plafond"]["display"] == "button"
    assert tiles["sensor.salon_temperature"]["display"] == "value"
    assert tiles["lock.entree"]["tap"]["type"] == "more_info"
    assert tiles["light.salon"]["longPress"]["type"] == "more_info"
    assert all(tile["size"] == "small" for tile in tiles.values())


def test_entity_registry_icon_becomes_a_material_symbol() -> None:
    document = generate()
    tiles = {
        section_item["entityId"]: section_item
        for room in document["rooms"]
        for section in room["sections"]
        for section_item in section["items"]
    }

    assert tiles["light.cuisine_plafond"]["icon"] == "ceiling_light"
    assert tiles["light.salon"]["icon"] == "lightbulb"  # icône de domaine


def test_generation_is_stable_for_the_same_home_assistant_state() -> None:
    first = generate()
    second = generate()

    assert [room["id"] for room in first["rooms"]] == [room["id"] for room in second["rooms"]]
    for room in first["rooms"]:
        for section in room["sections"]:
            for item in section["items"]:
                assert len(item["id"]) <= 64
                assert item["id"].startswith("tile_")


def test_an_empty_home_assistant_still_produces_a_valid_layout() -> None:
    from homeglass_server.ha.cache import HaCache

    document = generate_layout(HaCache()).model_dump(by_alias=True, mode="json")

    jsonschema.validate(document, SCHEMA)
    assert document["rooms"] == []
    assert [section["id"] for section in document["home"]["sections"]] == ["sec_favoris"]
