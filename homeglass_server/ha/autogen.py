"""Génération automatique d'une mise en page layout v1 depuis le cache Home Assistant.

Règles :

* une pièce par area ; les entités sans pièce sont regroupées dans une pièce « Autres » ;
* dans chaque pièce, une section par groupe logique de domaines (éclairage, climat…),
  dans l'ordre de :data:`DOMAIN_GROUPS` ;
* les entités techniques (``entity_category`` diagnostic/config), désactivées ou masquées
  dans Home Assistant sont exclues, ainsi que les domaines non visuels (automatisations,
  mises à jour, outils système) ;
* les scènes vont sur l'accueil, dans une section « Scènes » à dégradés variés ;
* l'accueil contient toujours une section « Favoris » vide, que l'utilisateur remplit
  ensuite depuis l'éditeur ;
* les identifiants sont dérivés des identifiants Home Assistant : stables et prévisibles,
  donc une nouvelle génération ne casse pas les liens de l'application.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from ..models.layout import (
    Action,
    ActionType,
    DisplayMode,
    Home,
    Layout,
    Room,
    SceneItem,
    SceneSection,
    TileItem,
    TileSection,
    TileSize,
)
from ..models.layout import EntityId  # noqa: F401 - documente le format attendu
from .cache import EntityRecord, HaCache

logger = logging.getLogger(__name__)

#: (clé de groupe, titre de section, domaines du groupe)
DOMAIN_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lights", "Éclairage", ("light",)),
    ("climate", "Climat", ("climate", "fan", "humidifier", "water_heater")),
    ("covers", "Volets", ("cover",)),
    ("media", "Multimédia", ("media_player", "camera")),
    ("security", "Sécurité", ("lock", "alarm_control_panel")),
    (
        "sensors",
        "Capteurs",
        ("sensor", "binary_sensor", "number", "select", "input_number", "input_select"),
    ),
    (
        "controls",
        "Commandes",
        (
            "switch",
            "input_boolean",
            "button",
            "input_button",
            "vacuum",
            "script",
            "weather",
            "person",
            "remote",
            "siren",
        ),
    ),
)

#: Domaines volontairement ignorés : ils ne se pilotent pas depuis un dashboard.
EXCLUDED_DOMAINS = frozenset(
    {
        "automation",
        "update",
        "group",
        "zone",
        "sun",
        "persistent_notification",
        "tts",
        "stt",
        "conversation",
        "tag",
        "timer",
        "counter",
        "input_text",
        "input_datetime",
        "todo",
        "calendar",
        "notify",
        "logger",
        "system_log",
        "homeassistant",
        "shell_command",
        "rest_command",
        "mqtt",
        "device_tracker",
        "geo_location",
        "proximity",
    }
)

#: Classes d'appareil d'un ``binary_sensor`` qui relèvent de la sécurité.
SECURITY_DEVICE_CLASSES = frozenset(
    {
        "door",
        "window",
        "opening",
        "motion",
        "occupancy",
        "presence",
        "smoke",
        "moisture",
        "gas",
        "carbon_monoxide",
        "safety",
        "tamper",
        "vibration",
    }
)

#: Icône Material Symbols par domaine (l'icône du registre Home Assistant est prioritaire).
DOMAIN_ICONS: dict[str, str] = {
    "light": "lightbulb",
    "switch": "toggle_on",
    "input_boolean": "toggle_on",
    "climate": "thermostat",
    "fan": "mode_fan",
    "humidifier": "humidity_percentage",
    "water_heater": "water_heater",
    "cover": "blinds",
    "media_player": "play_circle",
    "camera": "videocam",
    "lock": "lock",
    "alarm_control_panel": "shield",
    "sensor": "sensors",
    "binary_sensor": "sensors",
    "number": "pin",
    "input_number": "pin",
    "select": "list",
    "input_select": "list",
    "button": "smart_button",
    "input_button": "smart_button",
    "vacuum": "cleaning_services",
    "script": "play_arrow",
    "weather": "cloud",
    "person": "person",
    "remote": "settings_remote",
    "siren": "emergency",
}

#: Dégradés des cartes de scène, parcourus dans l'ordre pour varier les couleurs.
SCENE_GRADIENTS: tuple[tuple[str, str], ...] = (
    ("#FF9500", "#FF375F"),
    ("#5E5CE6", "#BF5AF2"),
    ("#30D158", "#0A84FF"),
    ("#FFD60A", "#FF9F0A"),
    ("#64D2FF", "#5E5CE6"),
    ("#FF2D55", "#FF6482"),
    ("#40C8E0", "#30D158"),
    ("#BF5AF2", "#FF375F"),
)

#: Domaines dont une tuile se contente d'un basculement.
TOGGLE_DOMAINS = frozenset({"light", "switch", "input_boolean", "fan"})

WIDE_DOMAINS = frozenset({"media_player", "camera", "climate"})

_ENTITY_ID_PATTERN = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_GROUP_DOMAINS = {domain: key for key, _, domains in DOMAIN_GROUPS for domain in domains}
_SECTION_TITLES = {key: title for key, title, _ in DOMAIN_GROUPS}
_KNOWN_DOMAINS = frozenset(_GROUP_DOMAINS) | {"scene"}

#: Valeurs de ``supported_features`` utiles (constantes Home Assistant).
SUPPORT_BRIGHTNESS = 1
SUPPORT_SET_POSITION = 4
SUPPORT_SET_SPEED = 1


def exclusion_reason(record: EntityRecord) -> str | None:
    """Pourquoi l'entité n'apparaît pas dans une mise de page générée (None si incluse)."""
    if record.disabled:
        return "désactivée dans Home Assistant"
    if record.hidden:
        return "masquée dans Home Assistant"
    if record.entity_category in {"diagnostic", "config"}:
        return f"catégorie {record.entity_category}"
    if record.domain in EXCLUDED_DOMAINS:
        return "domaine technique"
    if record.domain not in _KNOWN_DOMAINS:
        return "domaine non pris en charge"
    if not _ENTITY_ID_PATTERN.match(record.entity_id):
        return "identifiant hors format Home Assistant"
    if record.domain != "scene" and group_for(record) is None:
        return "aucun groupe"
    return None


def group_for(record: EntityRecord) -> str | None:
    """Groupe logique d'une entité, ou None si elle n'est pas affichable."""
    if record.domain == "binary_sensor" and record.device_class in SECURITY_DEVICE_CLASSES:
        return "security"
    if record.domain in {"lock", "alarm_control_panel"}:
        return "security"
    return _GROUP_DOMAINS.get(record.domain)


def generate_layout(
    cache: HaCache,
    *,
    published_at: datetime | None = None,
    revision: int = 1,
) -> Layout:
    """Construit un document layout v1 valide à partir du cache."""
    records = _visible_entities(cache)
    scenes = sorted((r for r in records if r.domain == "scene"), key=_sort_key)
    tiles = [record for record in records if record.domain != "scene"]

    by_area: dict[str | None, list[EntityRecord]] = {}
    for record in tiles:
        by_area.setdefault(cache.area_for(record), []).append(record)

    rooms: list[Room] = []
    for area_id in sorted(key for key in by_area if key is not None):
        area = cache.areas.get(area_id)
        room_id = _stable_id(f"room_{area_id}")
        rooms.append(
            Room(
                id=room_id,
                name=_label(area.name if area else area_id),
                icon=_icon(area.icon if area else None, "meeting_room"),
                area_id=area_id,
                sections=_sections(room_id, by_area[area_id]),
            )
        )

    orphans = by_area.get(None) or []
    if orphans:
        room_id = "room_autres"
        rooms.append(
            Room(
                id=room_id,
                name="Autres",
                icon="devices_other",
                area_id=None,
                sections=_sections(room_id, orphans),
            )
        )

    sections: list[TileSection | SceneSection] = []
    if scenes:
        sections.append(_scene_section(scenes))
    sections.append(TileSection(id="sec_favoris", kind="tiles", title="Favoris", items=[]))

    return Layout(
        revision=max(1, revision),
        published_at=published_at or datetime.now(timezone.utc),
        home=Home(title=_label(cache.location_name or "Maison"), sections=sections),
        rooms=rooms,
    )


def _visible_entities(cache: HaCache) -> list[EntityRecord]:
    return [
        record
        for record in cache.entities.values()
        if exclusion_reason(record) is None
    ]


def _sections(room_id: str, records: Iterable[EntityRecord]) -> list[TileSection]:
    grouped: dict[str, list[EntityRecord]] = {}
    for record in records:
        group = group_for(record)
        if group is None:
            continue
        grouped.setdefault(group, []).append(record)

    sections = []
    for key, _, _ in DOMAIN_GROUPS:
        items = grouped.get(key)
        if not items:
            continue
        sections.append(
            TileSection(
                id=_stable_id(f"sec_{room_id}_{key}"),
                kind="tiles",
                title=_SECTION_TITLES[key],
                items=[_tile(record) for record in sorted(items, key=_sort_key)],
            )
        )
    return sections


def _tile(record: EntityRecord) -> TileItem:
    return TileItem(
        id=_stable_id(f"tile_{record.entity_id.replace('.', '_')}"),
        entity_id=record.entity_id,
        name=_label(record.name or record.entity_id),
        icon=_icon(record.icon, DOMAIN_ICONS.get(record.domain, "devices_other")),
        accent=None,
        size=TileSize.WIDE if record.domain in WIDE_DOMAINS else TileSize.SMALL,
        display=_display_for(record),
        hidden=False,
        tap=_tap_for(record),
        long_press=Action(type=ActionType.MORE_INFO),
    )


def _tap_for(record: EntityRecord) -> Action:
    if record.domain in TOGGLE_DOMAINS:
        return Action(type=ActionType.TOGGLE)
    if record.domain == "script":
        return Action(
            type=ActionType.CALL_SERVICE,
            service="script.turn_on",
            target={"entity_id": record.entity_id},
        )
    return Action(type=ActionType.MORE_INFO)


def _display_for(record: EntityRecord) -> DisplayMode:
    features = record.supported_features
    if record.domain == "light":
        return DisplayMode.SLIDER if _is_dimmable(record) else DisplayMode.BUTTON
    if record.domain == "cover":
        return DisplayMode.SLIDER if features & SUPPORT_SET_POSITION else DisplayMode.BUTTON
    if record.domain == "fan":
        return DisplayMode.SLIDER if features & SUPPORT_SET_SPEED else DisplayMode.BUTTON
    if record.domain in {"sensor", "number", "input_number", "climate"}:
        return DisplayMode.VALUE
    return DisplayMode.BUTTON


def _is_dimmable(record: EntityRecord) -> bool:
    # ``brightness`` vaut souvent ``null`` quand la lampe est éteinte : la présence de la
    # clé ne suffit pas à conclure qu'elle est graduable.
    return (
        record.attributes.get("brightness") is not None
        or bool(record.attributes.get("supported_color_modes"))
        or bool(record.supported_features & SUPPORT_BRIGHTNESS)
    )


def _scene_section(scenes: list[EntityRecord]) -> SceneSection:
    items = []
    for index, record in enumerate(scenes):
        gradient = SCENE_GRADIENTS[index % len(SCENE_GRADIENTS)]
        items.append(
            SceneItem(
                id=_stable_id(f"scene_{record.entity_id.split('.', 1)[-1]}"),
                name=_label(record.name or record.entity_id),
                icon=_icon(record.icon, "auto_awesome"),
                gradient=list(gradient),
                actions=[
                    Action(
                        type=ActionType.CALL_SERVICE,
                        service="scene.turn_on",
                        target={"entity_id": record.entity_id},
                    )
                ],
            )
        )
    return SceneSection(id="sec_scenes", kind="scenes", title="Scènes", items=items)


def _sort_key(record: EntityRecord) -> tuple[str, str]:
    return (record.name.lower(), record.entity_id)


def _label(text: str) -> str:
    """Nom affichable : espaces normalisés, 120 caractères au plus."""
    collapsed = " ".join(str(text).split()) or "Sans nom"
    return collapsed if len(collapsed) <= 120 else collapsed[:119].rstrip() + "…"


def _icon(value: str | None, fallback: str) -> str:
    """Convertit ``mdi:ceiling-light`` en ``ceiling_light`` (Material Symbols)."""
    if not value:
        return fallback
    name = value.split(":", 1)[-1].strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name or fallback


def _stable_id(value: str) -> str:
    """Identifiant stable compatible avec le contrat (``StableId``, 64 caractères)."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        slug = "item"
    if slug[0].isdigit():
        slug = f"x_{slug}"
    return slug[:64]


__all__ = [
    "DOMAIN_GROUPS",
    "DOMAIN_ICONS",
    "EXCLUDED_DOMAINS",
    "SCENE_GRADIENTS",
    "exclusion_reason",
    "generate_layout",
    "group_for",
]
