"""Lancement réel : faux Home Assistant + serveur uvicorn sur des ports de test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))

from fake_ha import FakeHaServer  # noqa: E402

server = FakeHaServer()
fake = server.__enter__()

os.environ.update(
    {
        "HG_DATA_DIR": "/tmp/hglive/data",
        "HG_WEB_DIR": str(ROOT / "web"),
        "HG_SCHEMA_DIR": str(ROOT / "schema"),
        "HG_CORE_WS_URL": fake.supervisor_url,
        "HG_AUTH_WS_URL": fake.auth_url,
        "HG_INGRESS_PORT": "18099",
        "HG_API_PORT": "18765",
        "HG_LOG_LEVEL": "info",
        "SUPERVISOR_TOKEN": "supervisor-token",
        "HG_HA_UUID": "",
    }
)

from homeglass_server import serve  # noqa: E402

serve.main()
