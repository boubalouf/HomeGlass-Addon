from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from homeglass_server.models.schema_export import DEFAULT_OUTPUT, render_schema


def test_committed_schema_is_up_to_date() -> None:
    assert DEFAULT_OUTPUT.is_file(), "schema/layout.v1.json est absent (lancer l'export)"
    committed = DEFAULT_OUTPUT.read_text(encoding="utf-8")

    assert committed == render_schema(), (
        "schema/layout.v1.json ne correspond plus aux modèles : "
        "lancer `python -m homeglass_server.models.schema_export`"
    )


def test_committed_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "HomeGlass layout v1"


def test_example_validates_against_exported_schema(example_payload: dict) -> None:
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(example_payload)


def test_exported_schema_rejects_invalid_entity_id(tmp_path: Path, example_payload: dict) -> None:
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    example_payload["home"]["sections"][1]["items"][0]["entityId"] = "PasUneEntite"

    errors = list(Draft202012Validator(schema).iter_errors(example_payload))

    assert errors, "le schéma exporté devrait refuser un entityId invalide"
