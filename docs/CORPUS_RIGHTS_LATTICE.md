# The Corpus Rights Lattice

Phase I asked "what licence is this?" and matched the first pattern that
fit. That question has no answer for most real documents, because a
digitised book carries several licences at once and they are not the
same.

The lattice asks instead: *given everything each layer permits, what is
permitted overall?*

## The case that forced it

Standard Ebooks publishes this, verbatim, in one `dc:rights` field:

> The source text and artwork in this ebook are believed to be in the
> **United States public domain** … users located outside of the United
> States must check their local laws before using this ebook. The
> creators of, and contributors to, this ebook **dedicate their
> contributions** to the worldwide public domain via … CC0 1.0.

Two statements about two different things. The **work** is US public
domain. Standard Ebooks' **editorial contribution** — typesetting,
proofreading, markup — is CC0.

Phase I's engine reached CC0 first and produced a worldwide grant,
turning an explicit "check your local laws" into permission to
redistribute globally. That is not a regex bug to be fixed by reordering
patterns. It is the wrong question.

## Nine components

```python
class RightsComponent(StrEnum):
    WORK                    # the underlying text, as authored
    SOURCE_TEXT             # the specific source edition
    TRANSLATION             # a translation has its own copyright
    ILLUSTRATIONS
    EDITORIAL_CONTRIBUTION  # typesetting, proofreading, markup
    DIGITAL_EDITION
    METADATA
    SCAN                    # the photograph of the page
    OCR                     # the text extracted from that photograph
```

`CONTENT_COMPONENTS` — the ones that govern the ingested text —
deliberately excludes `METADATA` and `ILLUSTRATIONS`. Europeana's
metadata is CC0 and says nothing whatsoever about the object; an
illustration's licence does not govern the text you extracted.

`INSUFFICIENT_ALONE = {SCAN, OCR, METADATA}`. A library may hold rights
in its scan of a public-domain book, and an open licence on the *scan*
establishes nothing about the *work*. On its own, none of these three is
enough to accept anything.

## Three rules

```
intersect(stack) -> LatticeResult
```

1. **A blocking component blocks everything.** If any applicable layer is
   unknown, in copyright, or not evaluated, the result is blocked. There
   is no averaging and no majority vote.
2. **A permission survives only if every component grants it.**
   Commercial use, redistribution, and derivatives each survive the
   intersection only if unanimous.
3. **The narrowest scope wins.** `WORLDWIDE` ∩ `LOCAL_US_ONLY` =
   `LOCAL_US_ONLY`. Obligations accumulate rather than cancel:
   attribution and share-alike survive from any layer that imposes them.

`LatticeResult.limiting_components` names which layer produced the
narrowest answer, so "why is this LOCAL_US_ONLY" has a one-word reply.

## Worked example

```python
stack = RightsStack()
stack.add(RightsComponent.SOURCE_TEXT, "Public domain in the United States")
stack.add(RightsComponent.EDITORIAL_CONTRIBUTION, "CC0 1.0 Universal")

result = intersect(stack)
# effective_license      public-domain-us
# scope                  LOCAL_US_ONLY
# limiting_components    (SOURCE_TEXT,)
```

Accepted under `local_research_us`. Quarantined under
`release_worldwide`. Structurally, not by pattern ordering — verified
across 17 live Standard Ebooks editions, all 17 limited by `SOURCE_TEXT`.

## What "unknown" does and does not mean

This is the subtlest thing in the lattice, and getting it wrong in either
direction is expensive.

**An unknown licence quarantines.** Rule 1 exists because "we could not
establish this" must never read as "this is fine".

**A licence over one layer never stands in for a determination about
another.** This is the case that looks like an exception and is not.

A live Wikisource seed came back 30/30 quarantined, and the tempting
reading is that this is over-caution: CC BY-SA is an operative grant,
every contributor licenses their contribution under it, so surely what
the dump leaves unsaid about the underlying work can only widen freedom.

That reading is wrong. **A transcriber can only license what they own** —
the keystrokes, the proofreading, the markup. They do not own the work
they transcribed and cannot grant anyone rights in it. CC BY-SA over a
transcription of a text whose copyright status nobody has established
grants nothing usable: if the work is still in copyright, the composite
is unusable no matter what the footer says.

So the `WORK` layer is applicable to the ingested text and unevaluated,
which is exactly what the standing rule names. Rule 1 blocks it, and
30/30 quarantined is the right answer.

## Evidence

No accept without evidence. `decide()` enforces it as the last gate, and
it is absolute.

Evidence records what the provider actually said, where it said it, and a
hash of the statement, so a later audit can detect that a provider
*changed* its wording without re-parsing anything.

The absence of a licence is recorded as evidence too. `doab:dc:rights:absent`
means "we looked and there was nothing there", which an operator can act
on — by going to the publisher's page — whereas an empty stack is
indistinguishable from a parser that never ran.

**A rights stack must round-trip.** `RightsStack.from_dict` exists
because a stack rebuilt without its evidence intersects to the right
licence, the right scope, and the right limiting component — every field
an operator would look at is correct — and is then refused by the
evidence gate with no visible cause. Found by a live pilot that returned
`quarantine` on seventeen well-documented public-domain texts. The
round-trip test asserts the *decision*, not the licence; comparing
licences passes while the bug is present.

The stored licence is never re-derived on read. Re-normalising would let
a change in the licence vocabulary silently rewrite a stored verdict,
which is precisely what `corpus audit-rights` exists to make visible.

## Conditions of use are not licences

A licence says what you may do with the content. A **Content-Signal** in
robots.txt says what the publisher will and will not tolerate, and the
two are separate records.

`directory.doabooks.org` publishes:

```
User-agent: *
Content-Signal: search=yes,ai-train=no,use=reference
Allow: /
```

with a preamble declaring these express reservations of rights under
Article 4 of EU Directive 2019/790. The path rules permit this harvester.
The signal says its content may not be used to train models.

That is a condition on **use**, not on fetching, so obeying it at crawl
time and forgetting it afterwards would honour nothing at all. It is
parsed from the same robots.txt the fetcher already reads, recorded on
every document from that host, and carried into the export manifest as
`no-ai-training` and `use:reference`.

Every field is tri-state. The specification says an absent signal
"neither grants nor restricts", so `None` must not collapse into `True`
or `False` — the distinction between "they said no" and "they said
nothing" is the entire reason to record it.

## The standing rules

These are not negotiable and no amount of engineering pressure changes
them:

- Unknown, ambiguous, contradictory, absent, or unevaluated licence →
  **quarantine**.
- No quarantined document enters active retrieval.
- An old date, or an author's death date, is **not** sufficient proof on
  its own.
- No legal verdict rests on an LLM alone.
- "Open access" and `info:eu-repo/semantics/openAccess` are access
  models, not grants.
- Never weaken rights handling to increase the document count.

## Related

- [CORPUS_SOURCE_GRAPH.md](CORPUS_SOURCE_GRAPH.md) — which provider fills which role
- [CORPUS_RIGHTS_AND_EXPORT.md](CORPUS_RIGHTS_AND_EXPORT.md) — profiles and export gating
