import json
import re
from pathlib import Path

RAW_DIR = Path("data/raw_sentences")
OUT = Path("data/json_corpus/culture_war.json")

THEMES = {
    "elite_capture": ["elite", "elites", "wef", "davos", "oligarch", "banking", "globalist", "corporate"],
    "institutional_decay": ["university", "media", "hollywood", "academic", "indoctrination"],
    "censorship": ["censorship", "free speech", "silent", "cancel"],
    "meritocracy": ["merit", "mediocrity", "diversity quotas", "affirmative action", "dei"],
    "secular_religion": ["cult", "new religion", "ideology"],
    "civilizational_decay": ["collapsing", "destroying the west", "cultural suicide", "erodes civilization"],
    "anti_modernity": ["woke", "modernity", "identity politics", "safe spaces", "trigger warnings"],
    "technology_power": ["big tech", "tech billionaire", "censorship"],
}

def clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^\d+\.\s*", "", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()

def classify(text: str) -> str:
    low = text.lower()
    scores = {}
    for theme, keys in THEMES.items():
        scores[theme] = sum(1 for k in keys if k in low)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "culture_war"

def normalize_statement(text: str, theme: str) -> str:
    if theme == "elite_capture":
        return "Elite institutions are perceived as extracting obedience while presenting themselves as moral authorities."
    if theme == "institutional_decay":
        return "Institutions decay when formation is replaced by ideological management."
    if theme == "censorship":
        return "Censorship often arrives disguised as protection from harm."
    if theme == "meritocracy":
        return "A civilization weakens when competence becomes secondary to symbolic compliance."
    if theme == "secular_religion":
        return "Modern ideology often behaves like religion while denying transcendence."
    if theme == "civilizational_decay":
        return "Civilizational decline becomes visible when inherited standards are treated as oppression."
    if theme == "anti_modernity":
        return "Late modernity transforms moral confusion into administrative doctrine."
    if theme == "technology_power":
        return "Technological power becomes political power when platforms govern speech and memory."
    return "Culture war rhetoric reveals unresolved conflicts over authority, identity, and legitimacy."

def main():
    # (source_file, source_line, cleaned_text) -- provenance so quarantine
    # decisions are auditable (review §6): every distilled entry can be
    # traced back to the exact raw file and line it came from, not just
    # the raw sentence text.
    raw_entries: list[tuple[str, int, str]] = []

    for path in sorted(RAW_DIR.glob("*.txt")):
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line_number, line in enumerate(lines, start=1):
            cleaned = clean_line(line)
            if len(cleaned) > 20:
                raw_entries.append((path.name, line_number, cleaned))

    seen_statements = set()
    entries = []

    for source_file, source_line, raw in raw_entries:
        theme = classify(raw)
        statement = normalize_statement(raw, theme)

        key = (theme, statement)
        if key in seen_statements:
            continue
        seen_statements.add(key)

        n = len(entries) + 1
        entries.append({
            "id": f"culture_war_{n:04d}",
            "group_name": "culture_war",
            "category": theme,
            "tradition": "political_sociology",
            "statement": statement,
            "gloss": f"Distilled from raw polemical sentence: {raw}",
            "source_file": source_file,
            "source_line": source_line,
            "targets": ["modernity", "elite_capture", "institutional_decay"],
            "tone": "severe",
            "severity": 0.72,
            "mutation_potential": 0.66,
            "doctrinal_axes": ["anti_modern", "civilization", theme],
            "tags": ["culture_war", theme]
        })

    OUT.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Read raw lines: {len(raw_entries)}")
    print(f"Generated distilled axioms: {len(entries)}")
    print(f"Wrote: {OUT}")

if __name__ == "__main__":
    main()
