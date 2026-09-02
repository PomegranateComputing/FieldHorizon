from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .doctrinal import score_doctrinal_enforcement, score_symbolic_density
from .fragments import (
    MIN_WORDS,
    extract_field_fragment,
    extract_ngrams,
    has_truncation_marker,
    paragraph_count,
    word_count,
)

# Component weights, renormalized to sum to 1.0 now that source_alignment,
# json_alignment, and metadata_validity are gone (references are assembled
# in code, not scored from model echo -- see synthesis.build_structured_response).
DOCTRINAL_ENFORCEMENT_WEIGHT = 0.364
SYMBOLIC_DENSITY_WEIGHT = 0.182
LENGTH_WEIGHT = 0.272
STRUCTURE_WEIGHT = 0.182

# A 3-gram is "overused" once it shows up in more than this fraction of the
# canon-motif tracking window (canon.MOTIF_WINDOW canon fragments) -- see
# canon_loop_penalty.
MOTIF_FREQUENCY_RATIO = 0.3


@dataclass
class CycleEvaluation:
    symbolic_density: float
    symbolic_density_method: str
    doctrinal_enforcement: float
    doctrinal_enforcement_method: str
    length_score: float
    structure_score: float
    stuffing_penalty: float
    generic_penalty: float
    final_score: float
    verdict: str
    notes: list[str]


GENERIC_PHRASES = [
    "digital ashes",
    "ghost in the machine",
    "cyberspace",
    "vast tapestry",
    "ancient forces",
    "in the shadows",
]


def length_score(fragment: str) -> float:
    words = word_count(fragment)

    if words < 80:
        return 0.0
    if words < 140:
        return 0.35
    if words < MIN_WORDS:
        return 0.65
    if words < 450:
        return 1.0
    if words < 900:
        return 0.9

    return 0.75


def structure_score(fragment: str) -> float:
    paras = paragraph_count(fragment)
    words = word_count(fragment)

    if words < 120:
        return 0.0
    if paras >= 3:
        return 1.0
    if paras == 2:
        return 0.65
    return 0.35


def stuffing_penalty(fragment: str, json_rows: list[dict]) -> float:
    """
    Penalize fragments that merely dump categories/tags without developing them.
    """
    low = fragment.lower()
    words = max(1, word_count(fragment))

    labels = []
    for row in json_rows:
        labels.append(str(row.get("category", "")).lower())
        labels.append(str(row.get("group_name", "")).lower())

    labels = [x.replace("_", " ") for x in labels if x]
    hits = sum(1 for label in labels if label and label in low)

    density = hits / max(1, words / 80)

    return min(1.0, density / 4)


def generic_penalty(response: str) -> float:
    low = response.lower()
    hits = sum(1 for phrase in GENERIC_PHRASES if phrase in low)
    return min(1.0, hits / 4)



def canon_loop_penalty(text: str, motif_counts: dict[str, int] | None = None, window: int = 20) -> float:
    """
    Statistical replacement for a hardcoded phrase list (review's
    real-upgrade tier §2). The old list ("fans whir", "skulls", "officiant"
    ...) was a tombstone of the last motif collapse this system happened to
    have watched -- it could never catch the *next* one. This penalizes any
    3-gram in `text` whose frequency across the last `window` canon
    fragments (via canon.load_motif_counts) exceeds a fixed proportion of
    that window, so a new fixation is caught the moment it becomes
    statistically dominant instead of after someone notices and hardcodes it.

    `motif_counts` is None (no penalty) unless the caller supplies it --
    evaluate.py stays DB-free; cycle.py/multicycle.py load counts once via
    canon.load_motif_counts and pass them into every evaluate_cycle call.
    """
    if not motif_counts:
        return 0.0

    threshold = max(1, round(window * MOTIF_FREQUENCY_RATIO))
    grams = set(extract_ngrams(text, n=3))
    if not grams:
        return 0.0

    excess = sum(max(0, motif_counts.get(gram, 0) - threshold) for gram in grams)

    return min(0.35, excess * 0.07)


def evaluate_cycle(
    response: str,
    json_rows: list[dict] | None = None,
    motif_counts: dict[str, int] | None = None,
    cfg: AppConfig | None = None,
) -> CycleEvaluation:
    notes: list[str] = []
    json_rows = json_rows or []

    fragment = extract_field_fragment(response)
    words = word_count(fragment)
    paras = paragraph_count(fragment)

    enforcement, enforcement_method = score_doctrinal_enforcement(cfg, response, json_rows)
    density, density_method = score_symbolic_density(cfg, response)

    l_score = length_score(fragment)
    s_score = structure_score(fragment)
    stuff_penalty = stuffing_penalty(fragment, json_rows)
    g_penalty = generic_penalty(response)
    t_penalty = 0.35 if has_truncation_marker(fragment) else 0.0
    loop_penalty = canon_loop_penalty(fragment, motif_counts)

    if words < MIN_WORDS:
        notes.append(f"Fragment too short for canon: {words} words.")

    if paras < 2:
        notes.append(f"Fragment lacks structural depth: {paras} paragraph(s).")

    if enforcement < 0.7:
        notes.append("Weak doctrinal enforcement: retrieved axioms are cited but not sufficiently developed.")

    if stuff_penalty > 0.4:
        notes.append("Possible keyword stuffing: concepts listed more than developed.")

    if t_penalty > 0:
        notes.append("Fragment appears truncated.")

    if loop_penalty > 0:
        notes.append("Canon loop detected: repeated internal motifs are overused.")

    final_score = (
        enforcement * DOCTRINAL_ENFORCEMENT_WEIGHT
        + density * SYMBOLIC_DENSITY_WEIGHT
        + l_score * LENGTH_WEIGHT
        + s_score * STRUCTURE_WEIGHT
        - stuff_penalty * 0.15
        - g_penalty * 0.10
        - t_penalty
        - loop_penalty
    )

    final_score = max(0.0, min(1.0, final_score))

    # Hard gates. No cheap canonization. Humanity tried that already.
    canon_allowed = (
        words >= MIN_WORDS
        and paras >= 2
        and enforcement >= 0.75
        and t_penalty <= 0
        and loop_penalty < 0.20
    )

    if final_score >= 0.82 and canon_allowed:
        verdict = "CANON"
    elif final_score >= 0.68:
        verdict = "USEFUL_FRAGMENT"
    elif final_score >= 0.50:
        verdict = "HERESY"
    else:
        verdict = "NOISE"

    return CycleEvaluation(
        symbolic_density=round(density, 3),
        symbolic_density_method=density_method,
        doctrinal_enforcement=round(enforcement, 3),
        doctrinal_enforcement_method=enforcement_method,
        length_score=round(l_score, 3),
        structure_score=round(s_score, 3),
        stuffing_penalty=round(stuff_penalty, 3),
        generic_penalty=round(g_penalty, 3),
        final_score=round(final_score, 3),
        verdict=verdict,
        notes=notes,
    )


def evaluation_to_markdown(ev: CycleEvaluation) -> str:
    notes = "\n".join(f"- {n}" for n in ev.notes) if ev.notes else "- None"

    return f"""
## Evaluation

- verdict: {ev.verdict}
- final_score: {ev.final_score}
- symbolic_density: {ev.symbolic_density} ({ev.symbolic_density_method})
- doctrinal_enforcement: {ev.doctrinal_enforcement} ({ev.doctrinal_enforcement_method})
- length_score: {ev.length_score}
- structure_score: {ev.structure_score}
- stuffing_penalty: {ev.stuffing_penalty}
- generic_penalty: {ev.generic_penalty}

### Notes
{notes}
""".strip()
