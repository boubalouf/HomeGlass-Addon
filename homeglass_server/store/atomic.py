"""Écritures atomiques et lectures JSON partagées par les magasins de données.

Toute écriture passe par un fichier temporaire du même dossier, ``fsync`` puis
``os.replace`` : un arrêt brutal de l'add-on ne peut pas laisser un JSON tronqué, et un
lecteur concurrent voit soit l'ancien fichier, soit le nouveau, jamais un mélange.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def write_text_atomic(path: Path, text: str) -> None:
    """Écrit ``text`` dans ``path`` de façon atomique."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any | None:
    """Lit un JSON ; renvoie None s'il est absent ou illisible."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Fichier illisible %s (%s) : ignoré", path, error)
        return None


def _fsync_directory(directory: Path) -> None:
    """Rend le renommage durable (non supporté partout, jamais bloquant)."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["read_json", "write_json_atomic", "write_text_atomic"]
