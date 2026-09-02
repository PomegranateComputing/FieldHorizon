from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .config import AppConfig
from .server import create_app

# Implementation Brief IV, Phase UI-2 item 4: OpenAPI is the source of truth
# for the API contract (FABLE Sec.8.3) -- this is the one place that builds
# the schema, used both by the freeze script below and by the CI no-diff
# test (tests/test_openapi_contract.py) so the two can never drift apart.

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract" / "openapi.json"


def build_openapi_schema() -> dict:
    """
    The schema depends only on route/model definitions, not on any real data,
    but create_app() still calls init_db() on a real path -- a throwaway
    tmp directory keeps this from touching the project's actual database.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = AppConfig(
            root=root,
            database=root / "data.sqlite3",
            books=root / "books",
            json_corpus=root / "json_corpus",
            outputs=root / "outputs",
            logs=root / "logs",
            ollama_base_url="http://localhost:11434",
            default_model="hermes3:8b",
            temperature=1.25,
            top_p=0.95,
            repeat_penalty=1.08,
            num_ctx=8192,
            book_fragments=6,
            json_entries=8,
            chunk_chars=1800,
            chunk_overlap=250,
            tone="dark",
            mode="canonical_synthesis",
            manifestos=root / "manifestos",
            embedding_model="nomic-embed-text",
        )
        app = create_app(cfg, token="schema-export-placeholder")
        return app.openapi()


def render_schema(schema: dict) -> str:
    """Deterministic formatting so the frozen file's diff is meaningful, not noise from key ordering."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_contract(path: Path = CONTRACT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(build_openapi_schema()), encoding="utf-8")


if __name__ == "__main__":
    write_contract()
    print(f"Wrote {CONTRACT_PATH}")
