from __future__ import annotations

from fieldhorizon.rewrite import category_directives


def test_no_json_rows_produces_no_directives():
    assert category_directives(None) == ""
    assert category_directives([]) == ""


def test_directive_included_for_a_direct_category_match():
    directives = category_directives([{"category": "tawhid"}])
    assert "unity, idolatry, Allah, worship" in directives


def test_directive_included_via_domain_hint_membership():
    # elite_capture has no rewrite_directive of its own; it's a hint of
    # the culture_war domain, which does.
    directives = category_directives([{"category": "elite_capture"}])
    assert "elite capture, censorship, meritocracy" in directives


def test_directives_deduped_across_rows():
    directives = category_directives(
        [{"category": "modernity"}, {"category": "bureaucracy"}]
    )
    # modernity and bureaucracy share the identical directive text.
    assert directives.count("institutional control") == 1


def test_unknown_category_contributes_nothing():
    directives = category_directives([{"category": "totally_unknown_category"}])
    assert directives == ""
