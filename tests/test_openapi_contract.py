from __future__ import annotations

from fieldhorizon.openapi_export import CONTRACT_PATH, build_openapi_schema, render_schema


def test_frozen_contract_matches_the_live_schema():
    """
    contract/openapi.json is the frozen source of truth for generated
    TypeScript types (Implementation Brief IV, Phase UI-2 item 4). If this
    fails, a route or model changed without re-running
    `python -m fieldhorizon.openapi_export` -- regenerate and commit the diff.
    """
    assert CONTRACT_PATH.exists(), "contract/openapi.json is missing -- run `python -m fieldhorizon.openapi_export`"
    frozen = CONTRACT_PATH.read_text(encoding="utf-8")
    live = render_schema(build_openapi_schema())
    assert frozen == live
