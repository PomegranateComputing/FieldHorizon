# The Corpus Source Graph

Phase I assumed one source discovers a document, describes it, proves it
is legally reusable, and hosts the bytes. That assumption is wrong about
almost every institution worth harvesting, and Phase I could only express
its wrongness as a download failure.

This document explains the model that replaced it.

## Four roles, not one source

A document reaches the corpus only if four questions are all answered,
and nothing requires the same provider to answer them:

| Role | Question |
|---|---|
| `DISCOVERY` | Who told us this document exists? |
| `METADATA` | Who described it? |
| `RIGHTS_EVIDENCE` | Who published something we can point at to justify using it? |
| `CONTENT_HOSTING` | Who will serve the bytes? |

Four supporting capabilities qualify *how*: `RENDERED_TEXT` (the provider
supplies extracted text, so no parsing or OCR is needed),
`BULK_SNAPSHOT`, `INCREMENTAL_UPDATES`, and `SUPPORTS_RESUME`.

Each adapter declares what it does on the class:

```python
GALLICA_CAPABILITIES = SourceCapabilities(
    provider_id="gallica_text",
    capabilities=frozenset({
        Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
    }),
    # No CONTENT_HOSTING. See below.
)
```

Declared, not inferred. An adapter that claims `CONTENT_HOSTING` and then
cannot serve bytes should surface as a plan failure naming that provider,
not as a mysterious download error three stages later.

## What this looks like in practice

Every one of these was measured against the live service, not assumed.

### Gallica: discovery without content

`/SRU`, `/services/Pagination`, and the IIIF manifests all answer 200.
`/ark:/.../.texteBrut` answers **302 to an altcha proof-of-work
challenge**. Solving it would be bypassing an anti-bot protection, which
this project does not do.

So Gallica declares discovery, metadata, and rights evidence, and does
not declare content hosting. Its documents rest in `METADATA_ONLY`: real,
catalogued, rights-assessed, and not downloaded. The ALTO and
page-reconstruction parsers are written and tested offline, so if the
text interface ever opens, enabling it is configuration rather than new
code.

### DOAB → OAPEN: a directory and a repository

```
DOAB    DISCOVERY, METADATA              directory.doabooks.org
OAPEN   CONTENT_HOSTING, RENDERED_TEXT   library.oapen.org
----    RIGHTS_EVIDENCE                  nobody
```

That last line is the finding. Of 100 DOAB OAI records, none carried a
`dc:rights` element; of 12 items sampled through DOAB's REST API, six had
no rights field and five said `"open access"`; of 10 OAPEN items, none
carried rights metadata of any kind.

"Open access" is a business model. It says the reader pays nothing. It
grants no permission to redistribute, adapt, or index. So the bridge
resolves, the full text is reachable, and every document still
quarantines — which is the graph reporting what is actually there.

OAPEN is also the best content provider configured here, because it
attaches an already-extracted text layer in a DSpace `TEXT` bundle. No
PDF parsing, no scan-quality gamble, no OCR.

### Wikisource dumps: content that is somewhere else

`pages-articles` carries the main namespace. Wikisource keeps the
transcribed text of most works in the `Page:` namespace and assembles it
at render time, so a main-namespace entry is often
`<pages index="…djvu"/>` and nothing else. In a live seed, 176 of 206
pages were such shells.

They are counted and reported, not silently dropped. The same shape as
Gallica: discovery and metadata yes, content no.

### Europeana: a broker

Europeana describes objects held by hundreds of institutions and hosts
almost none of them. It declares no `CONTENT_HOSTING`; the content role
belongs to whichever provider `edmIsShownBy` names, and **that host must
be allowlisted on its own merits**. An aggregator followed blindly is an
allowlist bypass, because Europeana can name any host on the internet.

Rejected hosts are recorded per item, not just logged: "found 400,
fetchable 12" is what tells an operator which institutions to allowlist
next.

### Internet Archive: hosting without authority

Superb catalogue, unreliable rights. `licenseurl` reflects what an
uploader typed, and an uploader is not an authority on whether a work is
in the public domain. IA declares discovery, metadata, and content
hosting — and **not** rights evidence.

The narrow exception is `trusted_collections`: inside an allowlisted
institutional collection there is an identifiable institution behind the
statement. Empty by default, which quarantines everything.

## The states a document can rest in

Phase I had two outcomes: it worked, or it failed. Phase II adds eight
states, none of them terminal, that say what is actually missing.

| State | Meaning |
|---|---|
| `METADATA_ONLY` | Described, no content provider. A complete outcome. |
| `AUTH_REQUIRED` | A host exists but needs a credential that is not set. |
| `PROVIDER_UNVERIFIED` | A host was named that is not allowlisted. |
| `CONTENT_UNAVAILABLE` | Every candidate host was tried; none served it. |
| `ACCESS_BLOCKED` | The host answered and refused. |
| `OCR_PENDING` | The bytes are page images. |
| `BULK_SNAPSHOT_PENDING` | The content is in a dump not yet fetched. |
| `READY_FOR_DOWNLOAD` | Every role is filled. |

`METADATA_ONLY`, `AUTH_REQUIRED`, `BULK_SNAPSHOT_PENDING`, `OCR_PENDING`,
and `READY_FOR_DOWNLOAD` are in `NON_ERROR_ACQUISITION_STATES` and never
raise the error count. A source that describes documents it does not host
has done its job completely, and reporting that as a failure is what made
DOAB look broken in Phase I.

`ACCESS_BLOCKED` deserves its own note. Its legal transitions are exactly
`{QUEUED, METADATA_ONLY, FAILED_FINAL, WITHDRAWN}` — there is no path
from a refusal to `DOWNLOADING`. A 403 is the provider saying no, and the
only correct responses are to wait and to ask again later, never to ask
differently.

## Adding a source

1. Write the adapter against the provider's **published** interface.
   There is no generic web-scraper adapter and adding one would violate
   this project's terms of engagement with every source it harvests.
2. Declare `capabilities` honestly. If you have not verified that it
   serves bytes, do not claim `CONTENT_HOSTING`.
3. Capture fixtures from the live service, not from the documentation.
   Phase I's most expensive lesson was that fixtures written from docs
   disagree with reality in ways that only surface during a live run.
4. Add the source to `config/corpus_sources.yaml` with `enabled: false`
   and a `notes` block recording its terms of use.
5. Run a bounded live pilot. Read the result before enabling it.

## Related

- [CORPUS_RIGHTS_LATTICE.md](CORPUS_RIGHTS_LATTICE.md) — how layered rights are intersected
- [CORPUS_DUMP_OPERATIONS.md](CORPUS_DUMP_OPERATIONS.md) — bulk snapshot acquisition
- [CORPUS_SOURCE_POLICY.md](CORPUS_SOURCE_POLICY.md) — the standing rules for sources
