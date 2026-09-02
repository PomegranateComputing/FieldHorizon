# Corpus rights and export

How Field Horizon decides whether a document may enter the corpus, what
it records, and what stops the wrong material leaving in an export.

---

## 1. Two rules that explain the design

**No evidence, no accept.** `rights.evaluate()` cannot return `accept`
for a signal carrying no `Evidence`. Not "should not" — *cannot*: the
guard runs after every other branch and downgrades to quarantine. A
licence string nobody can point at a URL for is a rumour, not a licence.

**Age is not evidence.** A publication date of 1850 and an author who
died in 1890 are enriching context. They are never, on their own,
grounds for a public-domain verdict — the term of copyright depends on
jurisdiction, edition, translation, and editorial apparatus, none of
which a year can settle. `AGE_BASED_INFERENCE_ONLY` and
`AUTHOR_DEATH_INFERENCE_ONLY` exist to record that a decision was
*refused* on exactly this basis.

A third rule shapes every adapter: **metadata licence ≠ content
licence**. Europeana publishes its metadata under CC0; that says nothing
about the digital object. `RightsSignal` keeps the two apart and only
`content_license` is ever adjudicated.

No verdict is ever produced by an LLM. The engine is deterministic.

---

## 2. The decision

```json
{
  "decision": "accept|reject|quarantine",
  "normalized_license": "cc-by-sa",
  "rights_scope": "WORLDWIDE|LOCAL_US_ONLY|UNKNOWN",
  "commercial_use": true, "redistribution": true, "derivatives": true,
  "attribution_required": true, "share_alike": true,
  "jurisdictions": ["FR"],
  "evidence": [{"kind": "...", "value": "...", "url": "...",
                "content_hash": "...", "retrieved_at": "..."}],
  "reason_codes": ["CC_BY_SA"],
  "policy_profile": "local_research_us",
  "policy_version": "1.0.0",
  "evidence_checked_at": "2026-07-31T12:00:00+00:00",
  "notes": ""
}
```

Decisions are **append-only** in `corpus_rights_decisions`. An audit that
downgrades a document writes a *new* row; the old one remains as the
record of what was believed, and on what evidence, at the time it was
indexed. Refusals carry their evidence too — knowing *what* the provider
said is what makes a quarantine reviewable later.

Evidence is hashed, so a later audit can detect that a provider changed
a statement without having to diff free text.

---

## 3. Reason codes

**Accept:** `EXPLICIT_PUBLIC_DOMAIN`, `PUBLIC_DOMAIN_MARK`, `CC0`,
`CC_BY`, `CC_BY_SA`, `US_FEDERAL_GOVERNMENT_WORK`,
`EXPLICIT_OPEN_LICENSE`, `PROVIDER_DECLARED_CONTENT_LICENSE`.

**Refuse:** `UNKNOWN_LICENSE`, `NO_LICENSE_STATEMENT`,
`COPYRIGHT_NOT_EVALUATED`, `IN_COPYRIGHT`, `ALL_RIGHTS_RESERVED`,
`NON_COMMERCIAL_RESTRICTION`, `NO_DERIVATIVES_RESTRICTION`,
`ACCESS_RESTRICTED`, `BORROW_ONLY`, `CONTRADICTORY_METADATA`,
`VAGUE_FREE_CLAIM`, `AGE_BASED_INFERENCE_ONLY`,
`AUTHOR_DEATH_INFERENCE_ONLY`, `METADATA_LICENSE_ONLY`,
`PROVIDER_DISCLAIMS_WARRANTY`, `UNTRUSTED_UPLOADER_ASSERTION`,
`SCOPE_NOT_ESTABLISHED_WORLDWIDE`, `NOT_PERMITTED_BY_PROFILE`,
`EVIDENCE_STALE`, `EVIDENCE_MISSING`, `SOURCE_NOT_ALLOWLISTED`.

`reject` vs `quarantine`: **reject** is for licences with nothing
further to establish (In Copyright, All Rights Reserved, NC, ND,
borrow-only). **Quarantine** is for everything unresolved — unknown,
unevaluated, contradictory, metadata-only, evidence-missing,
profile-incompatible. Quarantine is reviewable; rejection is not.

---

## 4. Scope: the distinction that matters most

| Scope | Meaning |
|---|---|
| `WORLDWIDE` | The licence itself establishes worldwide scope (CC0, PDM, CC BY, CC BY-SA, unqualified public domain) |
| `LOCAL_US_ONLY` | Established for the United States only |
| `UNKNOWN` | Not established |

**The licence's inherent scope always wins.** A provider claiming
worldwide scope over a US-only determination does not upgrade it — the
limitation is in the determination, not in the provider's opinion of it.

Both Project Gutenberg and Standard Ebooks determine status under **US
law** and say so plainly. Their texts are `LOCAL_US_ONLY`:

| Profile | Verdict |
|---|---|
| `local_research_us` | **accept**, marked `LOCAL_US_ONLY` |
| `release_worldwide` | **quarantine** |
| `strict_public_domain` | **quarantine** |

### A bug worth remembering

Standard Ebooks writes, in one field: *"Public domain in the United
States. Users located outside of the United States must check their
local laws… Original content released to the public domain via the
Creative Commons CC0 1.0 Universal Public Domain Dedication."*

An early version matched the CC0 pattern first, normalized this to
worldwide CC0, and turned an explicit "check your local laws" into a
worldwide grant. The jurisdictional pattern now precedes CC0 in
`rights._TEXT_PATTERNS`, and the ordering is load-bearing. **If you edit
that list, keep the jurisdiction-limited patterns first.**

---

## 5. Audit and withdrawal

```bash
field-horizon corpus audit-rights --stale-after-days 30
```

Licence evidence goes stale. A document whose evidence has not been
re-verified within the window is marked `STALE` and **removed from the
active index immediately**.

What is preserved: the raw blob, the normalized text, the metadata
sidecar, every prior rights decision, and the full state history.
De-indexing removes a document from *retrieval*, not from the *record*
— files are never silently deleted.

`withdraw_document()` deletes the `sources` row (cascading to `chunks`
and their embeddings, concepts, entities, and motifs) after explicitly
clearing `chunks_fts`, which has no foreign key and would otherwise
leave ghost search results forever.

Run it from the timer, or on a schedule matching
`evidence_freshness_days` in `config/corpus_policy.yaml` (default 90).

---

## 6. The export guard

```bash
# Refuses, naming every offender
field-horizon corpus export-safe --rights-profile release_worldwide --output outputs/release_corpus

# Exports only what is permitted
field-horizon corpus export-safe --rights-profile release_worldwide --output outputs/release_corpus --filter
```

Without `--filter`, an incompatible document is a **hard error** listing
each one and why. The operator decides, not the tool. Exit code 2 —
a refusal, not a fault.

A document qualifies only if its **latest** rights decision is an accept
whose licence, scope, and permissions all satisfy the target profile.
The scope check is the one that matters: a Gutenberg text accepted under
`local_research_us` is `LOCAL_US_ONLY`, and `release_worldwide` requires
`WORLDWIDE`.

Output:

```
outputs/release_corpus/
  texts/books/…            texts/manifesto/…
  ATTRIBUTIONS.jsonl       ATTRIBUTIONS.md
  EXPORT_MANIFEST.json     # including every exclusion and its reasons
```

`allow_local_us_in_worldwide` exists in `config/corpus_policy.yaml` and
should stay `false`. Setting it true permits exactly the mistake the
`LOCAL_US_ONLY` scope exists to prevent.

---

## 7. Attribution

```bash
field-horizon corpus attributions
```

Writes `outputs/corpus/ATTRIBUTIONS.jsonl` and `.md`, grouped by
licence, recording per document: author, title, contributors,
translator, source, canonical URL, content URL, licence, licence
evidence URL, access date, verification date, whether attribution is
required, whether share-alike applies, distribution scope,
redistribution and commercial-use permissions, destination, language,
and normalized hash.

This is not decoration. CC BY and CC BY-SA impose real obligations, and
an export without this file would breach them — which is why
`export-safe` always writes one into the export directory.

Wikisource content is CC BY-SA 4.0: **attribution and share-alike both
apply**, and both are recorded per document.

---

## 8. Reviewing quarantine

```bash
field-horizon corpus quarantine --limit 50
field-horizon corpus show <document_id>
```

Every quarantined candidate also has a dossier at
`data/corpus/quarantine/<candidate_id>.json` containing the full
decision, its evidence, and its reason codes.

Nothing quarantined is ever indexed. `INDEXABLE_STATES` is
`{INGESTED, INDEXED}` and nothing else, and the accept gate is checked
three times independently — in the orchestrator, again in
`materialize()`, and again in `ingest_document()` — because each is
callable on its own and none may rely on another having checked.

To release a quarantined document, fix the underlying evidence problem
(a provider correction, an added collection allowlist, an API key) and
re-run discovery. There is deliberately no "force accept" command:
accepting without evidence is the one thing this engine exists to
prevent.
