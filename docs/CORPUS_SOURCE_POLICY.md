# Corpus source policy

Which catalogues Field Horizon talks to, why, and what it takes to add
one. Configuration lives in `config/corpus_sources.yaml`.

---

## 1. The rule

**Official, documented interfaces only.** OPDS feeds, OAI-PMH endpoints,
SRU search, published catalogue files, documented APIs.

There is no general web crawler in this subsystem and adding one is out
of scope by design — a test asserts that no registered adapter's name
contains "scrape", "crawl", or "generic_web". A source that cannot be
reached through an official interface is recorded as a disabled
candidate with the reason, not harvested anyway.

Never, under any circumstances: bypassing authentication, a paywall, a
CAPTCHA, or a rate limit. When Standard Ebooks' bulk feed started
requiring an account, the response was to switch to their public feed
and document the limitation — not to work around the 401.

---

## 2. Enabled sources

| Source | Adapter | Trust | Rights trusted | Interface |
|---|---|---:|:---:|---|
| Standard Ebooks | `standard_ebooks` | 0.95 | yes | Public Atom new-releases feed |
| Project Gutenberg | `gutenberg` | 0.90 | yes | `pg_catalog.csv` + per-book RDF |
| Project Gutenberg (fr) | `gutenberg` | 0.90 | yes | Same, filtered to French |
| Wikisource (fr, en) | `wikisource` | 0.80 | yes | MediaWiki Action API |
| DOAB | `oapen_doab` | 0.85 | yes | OAI-PMH |

### Standard Ebooks

Two things to understand, both verified against the live service.

**The bulk OPDS catalogue now returns HTTP 401** — it requires an
account. We have no credentials and do not bypass authentication, so the
source is pointed at the public `feeds/atom/new-releases` syndication
feed: official, unauthenticated, full rights text, `rel="enclosure"`
downloads — but only the 15 most recent releases. To harvest the full
catalogue, obtain an account and point `base_url` back at the OPDS feed.

**Their CC0 covers the *edition*, not the work.** Standard Ebooks
states: *"Public domain in the United States. Users located outside of
the United States must check their local laws before using this ebook.
Original content released to the public domain via the Creative Commons
CC0 1.0 Universal Public Domain Dedication."* The CC0 applies to their
typesetting, proofreading, and markup. The **work** is US public domain.
These texts are therefore `LOCAL_US_ONLY`.

### Project Gutenberg

The site is never crawled — its terms require that, and it publishes a
catalogue precisely so nobody needs to.

The published `pg_catalog.csv` has **no rights column** (`Text#, Type,
Issued, Title, Language, Authors, Subjects, LoCC, Bookshelves`), so
per-item rights cannot come from it. The adapter fetches each
candidate's official per-book RDF at `/ebooks/<id>.rdf`, which carries
`dcterms:rights`. That is one extra request per candidate, which is why
`page_size` is 25 and the rate is 6/min. A failed RDF fetch leaves the
candidate unlicensed and therefore quarantined — failing to confirm is
never the same as confirming.

Gutenberg determines public-domain status **under United States law**
and says so. Its texts normalize to `public-domain-us` with scope
`LOCAL_US_ONLY`. "Available on Project Gutenberg" is never worldwide
public domain.

The catalogue is ordered by accession and is overwhelmingly English, so
an unfiltered slice of it is monolingual. `gutenberg_fr` uses the same
adapter with `filter_languages: [fr]`. Both share gutenberg.org, and the
rate limiter is per-host, so together they still respect one 6/min
budget against the site.

### Wikisource

MediaWiki Action API, never the rendered site. Content is CC BY-SA 4.0
by the site's own terms, so **attribution and share-alike obligations
are real** and are recorded per document.

**Known limitation:** Wikimedia's `robots.txt` disallows `/w/`, which
includes `/w/api.php`. Since the harvester respects robots.txt, both
Wikisource sources currently discover nothing and report it honestly
rather than silently. The sanctioned bulk route is
`dumps.wikimedia.org`; the dump reader exists
(`wikimedia.iter_dump_pages`) but is not wired to a live dump download.
Setting `respect_robots: false` would let API discovery through, but
that is a decision for the operator to take deliberately, not a default.

Excluded editorially: talk pages, categories, indexes, templates, stubs,
redirects, non-content namespaces, and subpages of works already
imported via their parent (importing both duplicates the whole work).

### DOAB / OAPEN

OAI-PMH. Scholarly books whose full text carries an explicit open
licence.

DSpace repositories put the licence in `oaire:licenseCondition`, while
`dc:rights` carries an *access* statement — typically "open access" with
a COAR access-right URI. **"Open access" is a business model, not a
licence grant**, and is demoted so the rights engine quarantines rather
than accepts it. Of 92 live records: 33 accept, 40 reject (NC/ND), 19
quarantine.

**Known limitation:** `library.oapen.org` returns HTTP 403 to automated
clients, so records that pass the rights engine still fail at download.
403 is correctly treated as final rather than retried. In practice DOAB
is a metadata-and-rights source. Many other DOAB records point at the
publisher's own domain, which is not allowlisted — that is the allowlist
working as designed, since an aggregator that can redirect the harvester
to any host is an allowlist bypass.

---

## 3. Disabled sources

Configured, parser written and tested, **not enabled**.

| Source | Why disabled |
|---|---|
| Gallica / BnF | Parser and fixtures exist; awaiting a live integration test |
| Europeana | Requires `$EUROPEANA_API_KEY`, and its provider content hosts must be allowlisted individually first |
| Internet Archive | `trusted_for_content_rights: false`; needs an institutional collection allowlist before anything can be accepted |

### Gallica

Gallica separates the rights of the **work**, the **scan**, the **OCR
text**, and the **metadata**. Only an affirmative public-domain
statement about the object counts as a content licence here.
"Consultable en ligne" is an *access* statement — permission to look at
something on Gallica is not permission to harvest it — and is recorded
as `access_restricted`. Existing text mode (`.texteBrut`) is always
preferred; no OCR is ever run locally.

### Europeana

Europeana publishes its **metadata** under CC0, which says nothing
whatsoever about the digital object. This is the single largest source
of rights confusion in open-corpus work. The adapter records the two
separately and the rights engine quarantines "open metadata, no content
licence" with `METADATA_LICENSE_ONLY`.

Content is never fetched from Europeana — it comes from the providing
institution's own `edmIsShownBy` URL, and only when that host passes the
source's allowlist.

### Internet Archive

Deliberately the most conservative adapter here, and the only one that
defaults to `trusted_for_content_rights: false`.

Internet Archive is an extraordinary discovery surface and is **not** a
guarantor of the rights of what it hosts. Anyone can upload, and a
`licenseurl` field usually reflects what *the uploader* asserted. With
`allowed_collections` empty — the default — every item is quarantined
with `UNTRUSTED_UPLOADER_ASSERTION`. Only an item in an explicitly
allowlisted institutional collection, carrying an explicit open licence,
can be accepted.

Refused before rights are even considered: `access-restricted-item`, and
anything in `inlibrary`, `printdisabled`, `internetarchivebooks`, or
`lendinglibrary`. Only the best single text derivative is downloaded —
IA items routinely carry twenty derivatives and gigabytes of JP2
archives.

---

## 4. Candidate sources, recorded and not harvested

| Source | Status |
|---|---|
| Open Library (full text) | Metadata is excellent; full-text access is largely IA lending, which is borrow-only and out of scope |
| HathiTrust | Full-text access is membership-restricted and per-item rights are not exposed as a machine-readable content licence |
| Google Books | **Rejected for this purpose.** The API terms do not permit bulk text acquisition and per-object licensing is not verifiable |

Recorded so the decision is visible rather than forgotten. Also out of
scope by policy: repositories with no structured object licence,
activist or religious sites with no clear policy, abandonware sites,
blogs, forums, PDFs found via a search engine, secondary copies of works
available from an institution, and any site whose only legal argument is
the word "free".

---

## 5. Adding a source

A source stays `enabled: false` until **all five** exist:

1. its official endpoint is identified (never a scraped HTML page);
2. its terms of use are read and recorded in `notes`;
3. its licence mapping is implemented and tested;
4. its rate limits are configured;
5. an offline integration test covers its parser.

**If it speaks OPDS, OAI-PMH, or SRU, no code is needed** — add a
configuration block naming the `opds`, `oai_pmh`, or `oapen_doab`
adapter. Otherwise write an adapter in
`fieldhorizon/corpus/adapters/`, subclassing `SourceAdapter` and
implementing `discover_page`.

An adapter has three hard boundaries:

1. **Discovery never downloads content** — it reads catalogue records.
2. **An adapter never adjudicates rights** — it reports what the
   provider said, in a `RightsSignal`, *including where it said it*. The
   verdict is `rights.py`'s alone.
3. **An adapter never builds its own HTTP client** — it uses the
   injected fetcher, which is what makes rate limiting, robots.txt,
   redirect validation, and offline testing uniform and unforgettable.

Then: add fixtures under `tests/fixtures/corpus/`, write parser tests
driven by a `FixtureFetcher`, and add a config block. Set `enabled:
true` only after the tests pass and the live endpoint has been probed.

### Configuration fields that are enforced, not documentation

```yaml
- id: my_library
  adapter: opds                    # must be a registered adapter
  enabled: false
  base_url: "https://library.example.org/opds"
  allowed_hosts:                   # MANDATORY when enabled
    - library.example.org
    - .library.example.org         # leading dot = this host + subdomains
  trust: 0.7                       # 0..1 confidence in its CONTENT rights statements
  trusted_for_content_rights: true # false ⇒ quarantine everything on its word alone
  requests_per_minute: 10          # per-host pacing; Retry-After always wins
  max_download_bytes: 33554432
  allow_http: false                # per-source opt-in, for endpoints without TLS
  options: {}                      # adapter-specific
  notes: "Terms of use, licence position, and any caveats."
```

`allowed_hosts` has no default. A source with no host allowlist could
fetch anywhere, and a source whose domains nobody wrote down has not
been reviewed — so an enabled source without one is a hard configuration
error, not a permissive fallback.

---

## 6. Disabling a source

Set `enabled: false`. Already-harvested documents are unaffected —
disabling stops future discovery, it does not retract anything. To
remove already-harvested documents, see "Controlled deletion" in
[CORPUS_OPERATIONS.md](CORPUS_OPERATIONS.md).
