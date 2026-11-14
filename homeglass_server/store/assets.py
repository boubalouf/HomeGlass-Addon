"""Ressources image servies par ``GET /api/assets/{id}``.

L'éditeur envoie les octets bruts (``POST /internal/assets``), le type est déduit des
octets d'en-tête et non de l'en-tête ``Content-Type`` déclaré : un fichier texte renommé
``.png`` est refusé. Limite : 10 Mo (option ``max_asset_bytes``). SVG est volontairement
refusé (scriptable, donc risqué à servir à une application).

L'identifiant est le condensat du contenu : renvoyer deux fois la même image ne crée
qu'une ressource, et les URLs restent utilisables en cache « immutable ».
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from .atomic import read_json, write_json_atomic
from .repository import utc_now_string

logger = logging.getLogger(__name__)

MAX_ASSET_BYTES = 10 * 1024 * 1024
_ASSET_ID_PATTERN = re.compile(r"^asset_[0-9a-f]{16}$")
_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class AssetError(Exception):
    """Fichier refusé (format, taille) ou introuvable."""


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    """Renvoie ``(contentType, extension)`` d'après les octets d'en-tête."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


class AssetStore:
    """Ressources binaires immuables, indexées par condensat."""

    def __init__(self, root: Path, *, max_bytes: int = MAX_ASSET_BYTES) -> None:
        self.root = Path(root)
        self.max_bytes = max(1024, int(max_bytes))
        self.index_path = self.root / "index.json"

    # ------------------------------------------------------------------ écriture

    def save(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        declared_type: str | None = None,
    ) -> dict[str, Any]:
        if not data:
            raise AssetError("fichier vide")
        if len(data) > self.max_bytes:
            raise AssetError(
                f"fichier trop volumineux ({len(data)} octets, maximum {self.max_bytes})"
            )

        detected = detect_image_type(data)
        if detected is None:
            raise AssetError("format non pris en charge : PNG, JPEG, GIF ou WebP attendu")
        content_type, extension = detected

        if declared_type and declared_type != content_type:
            raise AssetError(
                f"le contenu est un {content_type} mais l'en-tête annonce {declared_type}"
            )

        checksum = hashlib.sha256(data).hexdigest()
        asset_id = f"asset_{checksum[:16]}"
        path = self.root / f"{asset_id}{extension}"

        index = self._load_index()
        existing = index.get(asset_id)
        if existing and path.is_file():
            return existing

        self.root.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        meta = {
            "id": asset_id,
            "filename": _clean_filename(filename, extension),
            "contentType": content_type,
            "size": len(data),
            "checksum": checksum,
            "createdAt": utc_now_string(),
        }
        index[asset_id] = meta
        write_json_atomic(self.index_path, index)
        logger.info("Ressource enregistrée : %s (%s, %d octets)", asset_id, content_type, len(data))
        return meta

    # -------------------------------------------------------------------- lecture

    def get(self, asset_id: str) -> tuple[Path, dict[str, Any]]:
        if not _ASSET_ID_PATTERN.match(asset_id or ""):
            raise AssetError("identifiant de ressource invalide")
        meta = self._load_index().get(asset_id)
        if not meta:
            raise AssetError(f"ressource inconnue : {asset_id}")
        path = _find_file(self.root, asset_id)
        if path is None:
            raise AssetError(f"fichier de la ressource introuvable : {asset_id}")
        return path, meta

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            self._load_index().values(), key=lambda meta: meta.get("createdAt") or "", reverse=True
        )

    def count(self) -> int:
        return len(self._load_index())

    # --------------------------------------------------------------------- interne

    def _load_index(self) -> dict[str, Any]:
        index = read_json(self.index_path)
        return index if isinstance(index, dict) else {}


def _find_file(root: Path, asset_id: str) -> Path | None:
    if not root.is_dir():
        return None
    for suffix in (".png", ".jpg", ".gif", ".webp"):
        candidate = root / f"{asset_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _clean_filename(filename: str | None, extension: str) -> str:
    cleaned = _FILENAME_PATTERN.sub("_", (filename or "").strip()).strip("._")
    if not cleaned:
        return f"image{extension}"
    return cleaned[:120]


__all__ = ["AssetError", "AssetStore", "MAX_ASSET_BYTES", "detect_image_type"]
