import json
from pathlib import Path

OUT = Path("data/json_corpus")
OUT.mkdir(parents=True, exist_ok=True)

categories = {
    "tawhid": [
        "Unity precedes multiplicity",
        "The One is not exhausted by representation",
        "Every idol begins as a compression algorithm"
    ],
    "logos": [
        "Meaning precedes administration",
        "Language creates worlds",
        "The Word judges systems by incarnation"
    ],
    "modernity": [
        "Efficiency often replaces wisdom",
        "Comfort can conceal decay",
        "Administration expands where meaning contracts"
    ],
    "apocalypse": [
        "Revelation is disclosure before destruction",
        "Collapse reveals hidden structures",
        "Judgment separates essence from noise"
    ],
    "red_flags": [
        "Every convenience has a metaphysical cost",
        "The machine remembers forgotten desires",
        "A society without transcendence invents substitutes"
    ]
}

entries = []

counter = 1

for category, statements in categories.items():
    for i in range(200):  # 5 x 200 = 1000
        base = statements[i % len(statements)]

        entries.append({
            "id": f"{category}_{counter:05d}",
            "group_name": category,
            "category": category,
            "tradition": "synthetic",
            "statement": base,
            "gloss": f"Variation {i}",
            "severity": round(0.4 + ((i % 50) / 100), 2),
            "mutation_potential": round(0.5 + ((i % 30) / 100), 2),
            "tags": [category]
        })

        counter += 1

with open(OUT / "generated_corpus.json", "w") as f:
    json.dump(entries, f, indent=2)

print(f"Generated {len(entries)} entries")
