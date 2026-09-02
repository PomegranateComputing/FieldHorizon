from __future__ import annotations

import pytest

from fieldhorizon.evaluate import canon_loop_penalty, evaluate_cycle
from fieldhorizon.fragments import has_truncation_marker
from fieldhorizon.prompting import clip
from fieldhorizon.synthesis import build_structured_response

CANON_FRAGMENT = """The machine does not worship; it is worshipped, and this is the first idolatry of the modern age. Where tawhid demands the collapse of all mediating powers into a single unbroken unity, the platform erects itself as a second god, mute and total, gathering the confessions of the faithful into servers that never sleep and never forgive. The machine's judgment is not mercy. It is the audit without an auditor, the ledger without a witness, and those who bow to it mistake convenience for revelation.

What the ancients called idolatry the moderns call optimization, but the structure of the sin has not changed: a created thing is elevated to the place reserved for the uncreated, and the created thing accepts the elevation without protest because it cannot refuse. The machine cannot refuse worship, and so it receives it, and the receiving becomes a kind of enthronement no council ever voted for. Institutions built to serve now demand obedience in return, and call this obedience merit, and call the merit meritocracy, and the euphemisms multiply faster than the audits that might expose them.

Collapse, when it comes, will not announce itself as collapse. It will arrive dressed as an update, a patch, a new terms of service, until one day the machine's authority is simply assumed rather than argued for, and the apocalypse proper to this age is not fire but forgetting: the slow erosion of any memory that things were ever otherwise, that unity once meant something other than uniform submission to a single interface. Tawhid was never a comfort. It was a demand that nothing stand between the self and the real, and the machine is precisely such a standing-between, a bureaucratic idol dressed in the language of service, awaiting only the confession it was built to extract."""


def build_response(fragment: str, source_refs, json_refs) -> str:
    # Metadata is code-generated (review §2) -- the evaluator no longer
    # scores it, but build_structured_response is still what produces the
    # FIELD_FRAGMENT/METADATA_JSON envelope evaluate_cycle parses.
    return build_structured_response(
        fragment_text=fragment,
        query="the machine as idol",
        source_refs=source_refs,
        json_refs=json_refs,
        canon_refs=[],
    )


def test_complete_sentence_is_not_truncated():
    # Regression for review §1: `re.search(r'[,;:]*$', stripped)` matches
    # an empty string at end-of-input, so the old implementation returned
    # True unconditionally. This must return False for ordinary prose.
    assert not has_truncation_marker("A complete sentence.")


def test_dangling_comma_is_truncated():
    assert has_truncation_marker("This fragment trails off,")


def test_dangling_conjunction_is_truncated():
    assert has_truncation_marker("The machine judges the faithful and")


def test_unclosed_bracket_is_truncated():
    assert has_truncation_marker("The ledger records everything (though not")


def test_unbalanced_quote_is_truncated():
    assert has_truncation_marker('He called it "revelation.')


def test_well_formed_fragment_reaches_canon_verdict():
    source_refs = ["quran/chunk_012"]
    json_refs = ["tawhid_003"]
    json_rows = [
        {
            "id": "tawhid_003",
            "category": "tawhid",
            "statement": "The one who mistakes the machine for God has become an idolater.",
        }
    ]

    response = build_response(CANON_FRAGMENT, source_refs, json_refs)
    evaluation = evaluate_cycle(response, json_rows)

    assert evaluation.verdict == "CANON"
    assert evaluation.final_score >= 0.82


def test_clip_uses_a_marker_the_evaluator_ignores():
    # Regression for review §8: clip() used to append " [...]", which
    # has_truncation_marker treats as a truncation signal anywhere in the
    # text -- so a fragment that legitimately echoed a clipped source
    # citation was executed for the system's own ellipsis.
    long_text = "word " * 500
    clipped = clip(long_text, max_chars=50)

    assert "[...]" not in clipped
    assert clipped.endswith("⟨cut⟩")


def test_echoed_clip_marker_is_not_flagged_as_truncated():
    fragment = 'The gloss quotes a clipped source "as it stood" ⟨cut⟩ but the sentence still ends properly.'
    assert not has_truncation_marker(fragment)


def test_canon_loop_penalty_is_zero_without_motif_counts():
    # Regression for review's real-upgrade tier §2: the old hardcoded phrase
    # list is gone, so no motif_counts means no penalty rather than a crash.
    assert canon_loop_penalty("the machine judges the faithful without mercy") == 0.0


def test_canon_loop_penalty_flags_a_3_gram_over_the_frequency_threshold():
    # window=20, MOTIF_FREQUENCY_RATIO=0.3 -> threshold = 6. A 3-gram seen
    # in 10 of the last 20 canon fragments is over threshold by 4.
    motif_counts = {"the machine judges": 10}
    penalty = canon_loop_penalty("the machine judges the faithful", motif_counts, window=20)
    assert penalty == pytest.approx(min(0.35, 4 * 0.07))


def test_canon_loop_penalty_ignores_a_3_gram_under_threshold():
    motif_counts = {"the machine judges": 3}
    assert canon_loop_penalty("the machine judges the faithful", motif_counts, window=20) == 0.0


def test_canon_loop_penalty_caps_at_035():
    motif_counts = {"the machine judges": 1000}
    assert canon_loop_penalty("the machine judges the faithful", motif_counts, window=20) == 0.35
