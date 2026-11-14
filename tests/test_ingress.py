from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI

from homeglass_server.ingress import (
    ObservingH11Protocol,
    describe_request_bytes,
    is_reserved_path,
    normalize_path,
    to_hex,
)
from homeglass_server.serve import build_config
from homeglass_server.settings import Settings


def asgi_request(
    app: FastAPI,
    path: str,
    headers: list[tuple[str, str]] | None = None,
    method: str = "GET",
) -> tuple[int, bytes]:
    """Envoie une requête ASGI brute, sans normalisation côté client HTTP."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers or []],
        "client": ("172.30.32.1", 41234),
        "server": ("0.0.0.0", 8099),
    }
    messages: list[dict] = []
    #: Avec la spec ASGI 2.3, Starlette écoute « http.disconnect » pendant l'envoi d'un
    #: FileResponse : le client reste connecté ici, et la tâche est annulée à la fin.
    disconnected = asyncio.Event()
    first_receive = True

    async def receive() -> dict:
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "/"),
        ("//", "/"),
        ("*", "*"),
        ("///", "/"),
        ("/index.html", "/index.html"),
        ("/api/ping", "/api/ping"),
        ("api/ping", "/api/ping"),
        ("//api//ping", "/api/ping"),
        ("/api/hassio_ingress/abc", "/"),
        ("/api/hassio_ingress/abc/", "/"),
        ("/api/hassio_ingress/abc/index.html", "/index.html"),
        ("/api/hassio_ingress/abc/api/ping", "/api/ping"),
        ("/api/hassio_ingress/abc/rooms/salon/", "/rooms/salon/"),
        ("/api/hassio_ingress", "/api/hassio_ingress"),
        ("/ingress/abc", "/"),
        ("/ingress/abc/", "/"),
        ("/ingress/abc/api/ping", "/api/ping"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert normalize_path(raw)[0] == expected


def test_normalize_path_reports_what_it_changed() -> None:
    assert normalize_path("/api/hassio_ingress/tok/")[1]
    assert normalize_path("//")[1]
    assert normalize_path("/")[1] == []


@pytest.mark.parametrize(
    ("path", "reserved"),
    [
        ("api/ping", True),
        ("api", True),
        ("internal/health", True),
        ("assets/editor.js", True),
        ("index.html", False),
        ("", False),
        ("rooms/salon", False),
        ("apix", False),
    ],
)
def test_reserved_prefixes(path: str, reserved: bool) -> None:
    assert is_reserved_path(path) is reserved


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], "HomeGlass Server"),
        ([("X-Ingress-Path", "/api/hassio_ingress/token")], "HomeGlass Server"),
        (
            [
                ("X-Ingress-Path", "/api/hassio_ingress/token"),
                ("X-Forwarded-Prefix", "/api/hassio_ingress/token"),
            ],
            "HomeGlass Server",
        ),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "",
        "//",
        "/",
        "/index.html",
        "/api/hassio_ingress/token",
        "/api/hassio_ingress/token/",
        "/api/hassio_ingress/token/index.html",
        "/ingress/token/",
        "/rooms/salon/",
    ],
)
def test_home_page_is_served_for_every_ingress_variant(
    app: FastAPI, path: str, headers: list[tuple[str, str]], expected: str
) -> None:
    status, body = asgi_request(app, path, headers)

    assert status == 200
    assert expected.encode() in body


def test_prefixed_api_path_still_reaches_the_api(app: FastAPI) -> None:
    status, body = asgi_request(app, "/api/hassio_ingress/token/api/ping")

    assert status == 200
    assert b'"schemaVersion":1' in body


@pytest.mark.parametrize("path", ["/api/unknown", "/internal/health", "/assets/editor.js"])
def test_reserved_paths_do_not_return_the_page(app: FastAPI, path: str) -> None:
    status, body = asgi_request(app, path)

    assert status == 404
    assert b"<html" not in body


def test_middleware_logs_raw_path_and_client(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="homeglass_server.ingress"):
        asgi_request(app, "/api/hassio_ingress/token/", [("X-Ingress-Path", "/api/hassio_ingress/token")])

    messages = [record.getMessage() for record in caplog.records]
    assert any("GET / —" in message for message in messages)
    assert any("172.30.32.1:41234" in message for message in messages)
    assert any("/api/hassio_ingress/token" in message for message in messages)
    assert any("Chemin Ingress réécrit" in message for message in messages)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"", "aucune donnée"),
        (b"\x16\x03\x01\x00\x2c\x01\x00\x00\x28", "TLS"),
        (b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n", "HTTP/2"),
        (b"\x00\x01\x02\x03\x04\x05\x06\x07", "binaire"),
        (b"HTTP/1.1 200 OK\r\n\r\n", "statut"),
        (b"GET / HTTP/1.1\r\nHost:\r\n\r\n", "malformée"),
        (b"BONJOUR\r\n\r\n", "illisible"),
    ],
)
def test_invalid_payload_description(data: bytes, fragment: str) -> None:
    assert fragment in describe_request_bytes(data)


def test_hex_dump_truncates_long_payloads() -> None:
    assert to_hex(b"\x16\x03\x01") == "16 03 01"
    long_dump = to_hex(b"\x16" * 100)

    assert long_dump.startswith("16 16 16")
    assert "100 octets reçus" in long_dump


async def _dummy_app(scope, receive, send) -> None:  # pragma: no cover - jamais appelée
    raise AssertionError("l'application ne doit pas être appelée par ce test")


def test_uvicorn_config_enables_access_log(settings: Settings) -> None:
    config = build_config(_dummy_app, settings)
    # uvicorn ne résout la classe de protocole qu'au chargement de la configuration.
    config.load()

    assert config.access_log is True
    assert config.log_config is None
    assert config.http_protocol_class is ObservingH11Protocol


@pytest.mark.parametrize(("log_level", "expected"), [("notice", "info"), ("fatal", "critical"), ("debug", "debug")])
def test_uvicorn_level_mapping(log_level: str, expected: str) -> None:
    config = build_config(_dummy_app, Settings(log_level=log_level))

    assert config.log_level == expected
