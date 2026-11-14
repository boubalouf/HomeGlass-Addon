"""Contrat de mise en page HomeGlass — schéma v1.

Règles structurantes du contrat :

* tous les champs optionnels ont une valeur par défaut explicite et l'API renvoie
  toujours la totalité des champs ;
* les identifiants sont stables, en minuscules (`snake_case` ou `kebab-case`) ;
* les couleurs sont au format ``#RRGGBB`` ou ``#AARRGGBB`` ;
* les icônes sont des noms Material Symbols en ``snake_case`` ;
* toute évolution cassante incrémente ``schemaVersion`` (nouveau module ``layout.v2``),
  les évolutions additives gardent la version 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = 1

#: "#RRGGBB" ou "#AARRGGBB"
HexColor = Annotated[str, Field(pattern=r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")]
#: Identifiant d'entité Home Assistant, par exemple "light.salon".
EntityId = Annotated[str, Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")]
#: Nom Material Symbols en snake_case, par exemple "meeting_room".
IconName = Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
#: Identifiant stable généré une fois puis jamais réutilisé.
StableId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
#: Service Home Assistant "domaine.service".
ServiceName = Annotated[str, Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")]
#: Identifiant d'area Home Assistant (slug).
AreaId = Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]


class BaseSchema(BaseModel):
    """Base commune : snake_case en Python, camelCase sur le fil, champs inconnus refusés."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class ThemeMode(str, Enum):
    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


class Density(str, Enum):
    COMPACT = "compact"
    STANDARD = "standard"
    COMFORTABLE = "comfortable"


class BackgroundType(str, Enum):
    GRADIENT = "gradient"
    SOLID = "solid"
    IMAGE = "image"


class TileSize(str, Enum):
    SMALL = "small"
    WIDE = "wide"
    LARGE = "large"


class DisplayMode(str, Enum):
    AUTO = "auto"
    BUTTON = "button"
    SLIDER = "slider"
    VALUE = "value"
    GRAPH = "graph"


class SectionKind(str, Enum):
    TILES = "tiles"
    SCENES = "scenes"


class ActionType(str, Enum):
    TOGGLE = "toggle"
    MORE_INFO = "more_info"
    CALL_SERVICE = "call_service"
    NAVIGATE = "navigate"
    NONE = "none"


class TabType(str, Enum):
    HOME = "home"
    ROOMS = "rooms"
    AUTOMATIONS = "automations"
    SETTINGS = "settings"


class SummaryChip(str, Enum):
    LIGHTS = "lights"
    CLIMATE = "climate"
    SECURITY = "security"
    MEDIA = "media"
    ENERGY = "energy"


class Background(BaseSchema):
    """Fond d'écran. ``assetId`` référence ``GET /api/assets/{id}``."""

    type: BackgroundType = BackgroundType.GRADIENT
    asset_id: str | None = None
    blur_dp: float = Field(default=0.0, ge=0.0, le=100.0)
    dim: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _image_requires_asset(self) -> "Background":
        if self.type is BackgroundType.IMAGE and not self.asset_id:
            raise ValueError("assetId est obligatoire quand background.type vaut 'image'")
        return self


class Theme(BaseSchema):
    mode: ThemeMode = ThemeMode.AUTO
    accent: HexColor = "#FF9500"
    tile_radius_dp: int = Field(default=22, ge=0, le=60)
    density: Density = Density.STANDARD
    background: Background = Field(default_factory=Background)

    @field_validator("accent")
    @classmethod
    def _normalize_accent(cls, value: str) -> str:
        return value.upper()


class Action(BaseSchema):
    """Action déclenchée par un tap, un appui long ou une carte de scène."""

    type: ActionType
    service: ServiceName | None = None
    data: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    room_id: StableId | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "Action":
        if self.type is ActionType.CALL_SERVICE and self.service is None:
            raise ValueError("service est obligatoire pour une action 'call_service'")
        if self.type is ActionType.NAVIGATE and self.room_id is None:
            raise ValueError("roomId est obligatoire pour une action 'navigate'")
        return self


class TileItem(BaseSchema):
    """Tuile d'appareil."""

    id: StableId
    entity_id: EntityId
    name: str = Field(min_length=1, max_length=120)
    icon: IconName = "devices_other"
    accent: HexColor | None = None
    size: TileSize = TileSize.SMALL
    display: DisplayMode = DisplayMode.AUTO
    hidden: bool = False
    tap: Action | None = None
    long_press: Action | None = None

    @field_validator("accent")
    @classmethod
    def _normalize_accent(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class SceneItem(BaseSchema):
    """Carte de scène : dégradé à deux couleurs et une ou plusieurs actions."""

    id: StableId
    name: str = Field(min_length=1, max_length=120)
    icon: IconName = "auto_awesome"
    gradient: list[HexColor] = Field(
        default_factory=lambda: ["#FF9500", "#FF375F"],
        min_length=2,
        max_length=2,
    )
    actions: list[Action] = Field(default_factory=list)

    @field_validator("gradient")
    @classmethod
    def _normalize_gradient(cls, value: list[str]) -> list[str]:
        return [color.upper() for color in value]


class TileSection(BaseSchema):
    id: StableId
    kind: Literal["tiles"] = "tiles"
    title: str = Field(min_length=1, max_length=120)
    items: list[TileItem] = Field(default_factory=list)


class SceneSection(BaseSchema):
    id: StableId
    kind: Literal["scenes"] = "scenes"
    title: str = Field(min_length=1, max_length=120)
    items: list[SceneItem] = Field(default_factory=list)


#: Une section est une union discriminée sur ``kind`` : les items sont typés en conséquence.
Section = Annotated[Union[TileSection, SceneSection], Field(discriminator="kind")]


class Home(BaseSchema):
    title: str = Field(default="Maison", min_length=1, max_length=120)
    summary_chips: list[SummaryChip] = Field(
        default_factory=lambda: [
            SummaryChip.LIGHTS,
            SummaryChip.CLIMATE,
            SummaryChip.SECURITY,
            SummaryChip.MEDIA,
        ]
    )
    sections: list[Section] = Field(default_factory=list)


class Room(BaseSchema):
    id: StableId
    name: str = Field(min_length=1, max_length=120)
    icon: IconName = "meeting_room"
    area_id: AreaId | None = None
    sections: list[Section] = Field(default_factory=list)


class Tab(BaseSchema):
    id: StableId
    type: TabType
    label: str = Field(min_length=1, max_length=40)
    icon: IconName = "apps"
    enabled: bool = True


class Layout(BaseSchema):
    """Document de mise en page complet, tel que renvoyé par ``GET /api/layout``."""

    schema_version: Literal[1] = 1
    revision: int = Field(default=1, ge=1)
    published_at: datetime
    theme: Theme = Field(default_factory=Theme)
    tabs: list[Tab] = Field(default_factory=lambda: _default_tabs())
    home: Home = Field(default_factory=Home)
    rooms: list[Room] = Field(default_factory=list)

    @field_validator("published_at")
    @classmethod
    def _force_utc(cls, value: datetime) -> datetime:
        # Le contrat impose de l'ISO 8601 UTC ; un horodatage naïf est interprété comme UTC.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_serializer("published_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_tabs() -> list[Tab]:
    """Onglets par défaut de l'application Android."""
    return [
        Tab(id="home", type=TabType.HOME, label="Maison", icon="home"),
        Tab(id="rooms", type=TabType.ROOMS, label="Pièces", icon="meeting_room"),
        Tab(id="auto", type=TabType.AUTOMATIONS, label="Automatisations", icon="bolt"),
        Tab(id="settings", type=TabType.SETTINGS, label="Réglages", icon="settings"),
    ]
