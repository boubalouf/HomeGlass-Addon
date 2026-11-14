"""DTO de l'API exposée à l'application Android et à l'éditeur.

Les noms restent en ``camelCase`` sur le fil (générateur d'alias de
[BaseSchema][homeglass_server.models.layout.BaseSchema]) ; seules les clés imposées par le
contrat (``ha_uuid``) gardent un alias explicite en ``snake_case``.
"""

from __future__ import annotations

from pydantic import Field

from .layout import SCHEMA_VERSION, BaseSchema, StableId, ThemeMode


class PingResponse(BaseSchema):
    """Réponse de ``GET /api/ping`` : découverte, aucune authentification requise.

    Le contrat impose la clé ``ha_uuid`` en snake_case (et ``schemaVersion`` en camelCase),
    d'où l'alias explicite qui neutralise le générateur d'alias de [BaseSchema].
    """

    app: str = "homeglass"
    version: str
    #: UUID de l'instance Home Assistant, résolu au démarrage (voir ha/instance.py).
    ha_uuid: str = Field(default="", alias="ha_uuid")
    schema_version: int = SCHEMA_VERSION


class PublicUser(BaseSchema):
    """Utilisateur authentifié, tel qu'exposé à l'application."""

    id: str
    name: str = ""
    is_admin: bool = False
    is_owner: bool = False
    source: str = "token"


class HaStatus(BaseSchema):
    """État de la connexion à Home Assistant."""

    connected: bool = False
    attempts: int = 0
    last_error: str | None = None
    connected_since: str | None = None
    version: str | None = None
    entities: int = 0
    areas: int = 0


class StatusResponse(BaseSchema):
    """État interne de l'add-on (``GET /internal/status``)."""

    version: str
    ha_uuid: str = Field(default="", alias="ha_uuid")
    schema_version: int = SCHEMA_VERSION
    ingress_port: int
    api_port: int | None = None
    published_revision: int | None = None
    draft_exists: bool = False
    devices: int = 0
    paired_devices: int = 0
    assets: int = 0
    ha: HaStatus = Field(default_factory=HaStatus)


class DeviceRegisterRequest(BaseSchema):
    """Corps de ``POST /api/devices/register``."""

    #: Absent au premier lancement : le serveur génère un identifiant stable.
    device_id: StableId | None = None
    name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    app_version: str | None = Field(default=None, max_length=40)
    android_version: str | None = Field(default=None, max_length=40)


class ProfileOverrides(BaseSchema):
    """Surcharges de mise en page propres à un appareil."""

    hidden_rooms: list[StableId] = Field(default_factory=list)
    hidden_tabs: list[StableId] = Field(default_factory=list)
    theme_mode: ThemeMode | None = None


class DeviceEntry(BaseSchema):
    """Appareil connu de l'add-on, avec son profil effectif."""

    device_id: str
    name: str | None = None
    model: str | None = None
    app_version: str | None = None
    android_version: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    profile: ProfileOverrides = Field(default_factory=ProfileOverrides)


class RegisterResponse(BaseSchema):
    """Réponse de ``POST /api/devices/register``."""

    device_id: str
    revision: int = 1
    profile: ProfileOverrides = Field(default_factory=ProfileOverrides)


class PairRequest(BaseSchema):
    """Corps de ``POST /api/pair/request`` : l'application s'annonce."""

    device_name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    app_version: str | None = Field(default=None, max_length=40)


class PairResponse(BaseSchema):
    """Réponse de ``POST /api/pair/request`` — volontairement **sans le code**."""

    pairing_id: str
    device_id: str
    expires_at: str
    expires_in: int
    code_length: int = 6
    server_version: str
    ha_uuid: str = Field(default="", alias="ha_uuid")


class PairClaimRequest(BaseSchema):
    """Corps de ``POST /api/pair/claim`` : le code lu dans Home Assistant."""

    pairing_id: str = Field(min_length=4, max_length=64)
    code: str = Field(min_length=4, max_length=12)


class PairClaimResponse(BaseSchema):
    """Réponse de ``POST /api/pair/claim`` : jeton d'appareil, à conserver côté application."""

    token: str
    device_id: str
    device_name: str | None = None
    server_version: str
    ha_uuid: str = Field(default="", alias="ha_uuid")


class AutogenerateRequest(BaseSchema):
    """Corps de ``POST /internal/layout/autogenerate``."""

    #: Publier directement le résultat (révision +1) au lieu de le renvoyer seulement.
    publish: bool = False


class AssetMeta(BaseSchema):
    """Ressource image stockée par l'add-on."""

    id: str
    filename: str | None = None
    content_type: str
    size: int
    checksum: str
    created_at: str | None = None


__all__ = [
    "AssetMeta",
    "AutogenerateRequest",
    "DeviceEntry",
    "DeviceRegisterRequest",
    "HaStatus",
    "PairClaimRequest",
    "PairClaimResponse",
    "PairRequest",
    "PairResponse",
    "PingResponse",
    "ProfileOverrides",
    "PublicUser",
    "RegisterResponse",
    "StatusResponse",
]
