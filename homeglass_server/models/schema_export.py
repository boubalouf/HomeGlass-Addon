"""Export du JSON Schema de la mise en page, généré depuis les modèles Pydantic.

Usage :
    python -m homeglass_server.models.schema_export            # écrit schema/layout.v1.json
    python -m homeglass_server.models.schema_export --check    # vérifie que le fichier est à jour
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .layout import SCHEMA_VERSION, Layout

SCHEMA_ID = "https://homeglass.local/schema/layout.v1.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "schema" / "layout.v1.json"


def build_schema() -> dict:
    """Construit le document JSON Schema (draft 2020-12) à partir des modèles."""
    if SCHEMA_VERSION != 1:  # garde-fou : le fichier exporté doit rester layout.v1.json
        raise RuntimeError("SCHEMA_VERSION a changé : créer schema/layout.vN.json")

    schema = Layout.model_json_schema(by_alias=True)
    schema.pop("title", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "HomeGlass layout v1",
        **schema,
    }


def render_schema() -> str:
    return json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"


def write_schema(path: Path = DEFAULT_OUTPUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exporte le JSON Schema du contrat HomeGlass v1.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="ne rien écrire, échouer si le fichier versionné diffère des modèles",
    )
    args = parser.parse_args(argv)

    expected = render_schema()
    if args.check:
        if not args.output.is_file():
            print(f"absent : {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != expected:
            print(f"obsolète : {args.output} (relancer sans --check)", file=sys.stderr)
            return 1
        print(f"à jour : {args.output}")
        return 0

    write_schema(args.output)
    print(f"écrit : {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
