import json
from pathlib import Path

OUT = Path("data/json_corpus")
OUT.mkdir(parents=True, exist_ok=True)

DOMAINS = {
    "modernity": [
        "Modernity converts inherited meaning into managed preference.",
        "The modern subject is trained to call dependency freedom.",
        "Progress becomes theology when it forgets death.",
        "Comfort is the narcotic form of collapse.",
        "The modern world does not abolish ritual; it makes ritual unconscious.",
        "A society that mocks sacrifice becomes unable to understand inheritance.",
        "Modernity mistakes motion for ascent.",
        "The cult of innovation hides a terror of judgment.",
        "When transcendence is denied, entertainment becomes liturgy.",
        "The future is often a marketing department wearing prophetic robes."
    ],
    "bureaucracy": [
        "Every expanding administration tends to replace judgment with procedure.",
        "Metrics become idols when institutions forget their purpose.",
        "A form is a prayer addressed to a dead institution.",
        "Bureaucracy is the ghost of order after authority has fled.",
        "The administrator fears exception because exception demands soul.",
        "Procedure is mercy only when wisdom governs it.",
        "Where responsibility disappears, workflow multiplies.",
        "The spreadsheet is the monastery of managerial civilization.",
        "A ticketing system is confession without absolution.",
        "An institution dies when compliance becomes its highest virtue."
    ],
}

entries = []

for domain, statements in DOMAINS.items():
    for i, statement in enumerate(statements, start=1):
        entries.append({
            "id": f"{domain}_{i:04d}",
            "group_name": domain,
            "category": domain,
            "tradition": "synthetic_doctrinal",
            "statement": statement,
            "gloss": f"{domain} axiom {i}",
            "targets": ["modernity", "machine", "civilization"],
            "tone": "severe",
            "severity": 0.75 + ((i % 5) * 0.04),
            "mutation_potential": 0.70 + ((i % 4) * 0.05),
            "doctrinal_axes": [domain, "anti_modern", "field_horizon"],
            "tags": [domain, "v2", "doctrinal"]
        })

path = OUT / "doctrinal_v2.json"
path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"Generated {len(entries)} entries -> {path}")
