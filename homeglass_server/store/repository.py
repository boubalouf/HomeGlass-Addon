"""Brouillon, version publiée et historique des mises en page.

Disposition sur disque, sous le dossier de données de l'add-on (``/data``) :

::

    layout/draft.json          brouillon en cours d'édition
    layout/published.json      version servie à l'application Android
    layout/history/000042.json 20 dernières versions publiées (000001.json, …)

Chaque publication valide le document contre le contrat Pydantic, incrémente ``revision``,
horodate ``publishedAt`` et écrit d'abord l'historique puis la version courante : une
publication interrompue laisse l'ancienne version intacte.

``migrate()`` prépare les évolutions de schéma : les documents ``schemaVersion`` 1 passent
tels quels, un document plus récent que l'add-on est refusé avec un message explicite.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..models.layout import SCHEMA_VERSION, Layout
from .atomic import read_json, write_json_atomic

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_HISTORY_NAME = re.compile(r"^(\d+)\.json$")


class StoreError(Exception):
    """Erreur de validation ou d'état du magasin de mises en page."""


def utc_now_string(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(TIME_FORMAT)


def migrate(payload: Any) -> dict[str, Any]:
    """Amène un document à ``schemaVersion`` 1 (identité aujourd'hui) ou refuse."""
    if not isinstance(payload, dict):
        raise StoreError("la mise en page doit être un objet JSON")

    version = payload.get("schemaVersion", SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise StoreError(f"schemaVersion invalide : {version!r}")
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"schemaVersion {version} inconnu : mettez à jour l'add-on HomeGlass Server"
        )

    document = dict(payload)
    while version < SCHEMA_VERSION:
        step: Callable[[dict[str, Any]], dict[str, Any]] | None = MIGRATIONS.get(version)
        if step is None:
            raise StoreError(f"aucune migration de schemaVersion {version} vers la suivante")
        document = step(document)
        version = document.get("schemaVersion", version + 1)
    document["schemaVersion"] = SCHEMA_VERSION
    return document


#: Migrations à écrire le jour où le contrat passera en v2 : ``{1: _v1_to_v2}``.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class LayoutStore:
    """Dépôt fichier des mises en page, avec historique borné."""

    def __init__(self, root: Path, *, history_limit: int = 20) -> None:
        self.root = Path(root)
        self.layout_dir = self.root / "layout"
        self.history_dir = self.layout_dir / "history"
        self.draft_path = self.layout_dir / "draft.json"
        self.published_path = self.layout_dir / "published.json"
        self.history_limit = max(1, int(history_limit))

    # ------------------------------------------------------------------ validation

    def validate(self, payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
        """Valide un document et renvoie sa forme sérialisée complète (tous les champs)."""
        document = migrate(payload)
        document.setdefault("revision", 1)
        document.setdefault("publishedAt", utc_now_string(now))
        try:
            return Layout.model_validate(document).model_dump(by_alias=True, mode="json")
        except ValidationError as error:
            raise StoreError(_describe_errors(error)) from error

    # ------------------------------------------------------------------- brouillon

    def get_draft(self) -> dict[str, Any] | None:
        return read_json(self.draft_path)

    def save_draft(self, payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
        """Valide puis enregistre le brouillon."""
        document = self.validate(payload, now=now)
        write_json_atomic(self.draft_path, document)
        logger.info("Brouillon enregistré (revision %s)", document.get("revision"))
        return document

    # ------------------------------------------------------------------- publication

    def get_published(self) -> dict[str, Any] | None:
        return read_json(self.published_path)

    def publish(self, payload: Any | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Publie ``payload`` (ou le brouillon, ou la version courante) en révision +1."""
        document = payload
        if document is None:
            document = self.get_draft() or self.get_published()
            if document is None:
                raise StoreError("aucun brouillon à publier")

        validated = self.validate(document, now=now)
        current = self.get_published() or {}
        revision = int(current.get("revision") or 0) + 1
        validated["revision"] = revision
        validated["publishedAt"] = utc_now_string(now)

        # Historique d'abord : si l'écriture suivante échoue, la version servie reste
        # cohérente et l'historique contient déjà la nouvelle entrée.
        write_json_atomic(self.history_dir / f"{revision:06d}.json", validated)
        write_json_atomic(self.published_path, validated)
        self._prune_history()
        logger.info("Mise en page publiée en révision %d", revision)
        return validated

    def history(self) -> list[dict[str, Any]]:
        """Versions disponibles, de la plus récente à la plus ancienne."""
        versions: list[dict[str, Any]] = []
        if not self.history_dir.is_dir():
            return versions
        for path in self.history_dir.iterdir():
            match = _HISTORY_NAME.match(path.name)
            if not match:
                continue
            document = read_json(path)
            if not isinstance(document, dict):
                continue
            versions.append(
                {
                    "revision": int(match.group(1)),
                    "publishedAt": document.get("publishedAt"),
                    "rooms": len(document.get("rooms") or []),
                }
            )
        versions.sort(key=lambda item: item["revision"], reverse=True)
        return versions

    def get_version(self, revision: int) -> dict[str, Any] | None:
        return read_json(self.history_dir / f"{int(revision):06d}.json")

    def restore(self, revision: int, *, now: datetime | None = None) -> dict[str, Any]:
        """Republie une version de l'historique en tant que nouvelle révision."""
        document = self.get_version(revision)
        if document is None:
            raise StoreError(f"révision {revision} absente de l'historique")
        return self.publish(document, now=now)

    # --------------------------------------------------------------------- interne

    def _prune_history(self) -> None:
        """Ne conserve que les ``history_limit`` versions les plus récentes."""
        if not self.history_dir.is_dir():
            return
        revisions = sorted(
            int(match.group(1))
            for path in self.history_dir.iterdir()
            if (match := _HISTORY_NAME.match(path.name))
        )
        for revision in revisions[: max(0, len(revisions) - self.history_limit)]:
            try:
                (self.history_dir / f"{revision:06d}.json").unlink()
            except OSError as error:
                logger.warning("Purge de l'historique impossible (%s) : %s", revision, error)


def _describe_errors(error: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(part) for part in item['loc']) or 'document'} : {item['msg']}"
        for item in error.errors()[:5]
    ]
    return "mise en page invalide — " + " ; ".join(problems)


__all__ = [
    "LayoutStore",
    "MIGRATIONS",
    "StoreError",
    "TIME_FORMAT",
    "migrate",
    "utc_now_string",
]
