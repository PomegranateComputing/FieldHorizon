# Corpus Harvester — Phase II Acceptance Report

**Source Graph, Rights Lattice and Institutional Scale**

| | |
|---|---|
| Branch | `feature/autonomous-open-corpus-phase-ii` |
| Parent | `1bc5173` (end of Phase I) |
| Commits | 15 |
| Date | 2026-08-01 |
| Tests | 1288 passing, every outbound socket blocked |
| Schema | version 6, additive |
| New dependencies | none |
| systemd timer | not installed, not enabled, not active |

---

## 1. What changed

Phase I assumed one source discovers a document, describes it, proves it
is legally reusable, and hosts the bytes. That assumption is false about
almost every institution worth harvesting, and Phase I could only express
its falseness as a download failure.

Phase II replaces it with an **acquisition graph**: four roles that any
combination of providers may fill, a **rights lattice** that intersects
per-component licences instead of matching one pattern, and eight new
states so a document can rest somewhere honest instead of failing.

---

## 2. The live pilot

Run 2026-08-01 against the real services. Well inside the brief's
ceilings of 100 documents and 5 GB.

```
78 documents · 47 requests · 1,610,178 bytes (1.6 MB) · 201 seconds
```

### Standard Ebooks (GitHub)

```
10 documents · 22 requests · 261 kB · 41s
10/10  public-domain-us  LOCAL_US_ONLY  us=accept  world=quarantine
10/10  limited by SOURCE_TEXT
```

The mandatory lattice case, in production data. One `dc:rights` field
states the **work** is US public domain and, separately, that Standard
Ebooks' **editorial contribution** is CC0. Phase I's engine reached CC0
first and produced a worldwide grant — turning "check your local laws"
into permission to redistribute globally. Every one of the ten is now
`LOCAL_US_ONLY` because `SOURCE_TEXT` limits it, structurally rather than
by pattern ordering.

An earlier run of 17 editions gave the same result, 17/17.

### Wikisource (frwikisource dump)

```
20 documents · 7 requests · 848 kB of 2,801,978,811 (0.0303%) · 31s
20/20  unknown  us=quarantine  world=quarantine, all limited by WORK
0/20   carried a per-work determination
176 transclusion shells skipped
```

The bounded-cost claim, measured: **three hundredths of one percent** of
a 2.8 GB dump, via a 512 kB index prefix and one Range request per
100-page bzip2 stream.

The 176 shells are the finding. Wikisource keeps transcribed text in the
`Page:` namespace and assembles it at render time, so most
main-namespace entries are `<pages index="…djvu"/>` and nothing else —
complete, correct pages containing no text. Counted and reported, never
silently dropped.

### DOAB → OAPEN

```
40 documents · 17 requests · 475 kB · 116s
1 bridged to OAPEN, with pre-extracted text
49 records with no content provider at all
8/8 sampled  unknown  us=quarantine  world=quarantine, limited by WORK
Content-Signal: search=yes,ai-train=no,use=reference
```

The bridge resolves and every document still quarantines, because **no
provider fills the rights role**. Measured: 0 of 100 DOAB OAI records
carried `dc:rights`; of 12 DOAB REST items, six had no rights field and
five said `"open access"`; of 10 OAPEN items, none carried rights
metadata of any kind.

"Open access" is a business model — the reader pays nothing. It grants no
permission to redistribute, adapt, or index.

*(The 49 counts every record on the OAI page; `documents` is capped at 40
for reporting.)*

### Gallica

```
8 documents · 1 request · 26 kB · 14s
0 downloaded — BY DESIGN
```

```
refused as declared: Gallica text mode for ark:/12148/bpt6k57100852 is
behind an altcha anti-bot challenge. The harvester does not solve
challenges. This document remains METADATA_ONLY with full rights
evidence recorded.
```

The clearest demonstration of the whole phase. SRU, Pagination, and IIIF
answer 200; `.texteBrut` answers 302 to a proof-of-work challenge.
Gallica therefore declares discovery, metadata, and rights evidence and
**not** content hosting, and its documents rest in `METADATA_ONLY`. The
ALTO and page-reconstruction parsers exist and are tested offline, so if
that interface ever opens, enabling it is configuration rather than code.

**Gallica is now enabled**, and downloads nothing. Re-run end to end
through the real orchestrator after the acquisition plan was wired in
(§8), against a fresh data root:

```
sources: ['gallica_text']
STATES       {'METADATA_ONLY': 6}
PLANS        {'METADATA_ONLY': 6}
plan_parked  {'METADATA_ONLY': 6}
errors 0 · downloaded 0 · quarantined 0
1 request · 16,347 bytes · 2s
```

Six documents discovered, six planned, six parked, nothing downloaded and
nothing recorded as an error. That is a complete acquisition for a
discovery-and-rights provider.

---

## 3. Defects found by the pilots, and fixed

Each was found by running against a live service, not by reasoning.

| # | Defect | Consequence if shipped |
|---|---|---|
| 1 | `RightsStack` serialised but could not be read back | Every document reloaded from the database quarantined, with every visible field correct and no explanation. Seventeen well-documented public-domain texts came back `quarantine`. |
| 2 | Wikisource `WORK` layer briefly made non-blocking | Would have accepted 30 documents on a transcriber's licence over a work they do not own. Reverted — see §4. |
| 3 | Multistream index buffered whole | 28 MB compressed, expands past 200 MB, to read 30 titles. |
| 4 | MARC roles kept only the last value | The live fixture gives a translator `ann`, `trl`, `wpr`; keeping the last made Aylmer Maude a writer-of-preface and dropped the translation layer entirely. |
| 5 | API subpage rule applied to dumps | Imported **exactly zero** documents. A dump has no rendered output. |
| 6 | Rich markup ate `[ocr]` in the health row | Printed `pip install 'fieldhorizon'` — an instruction that runs cleanly and installs the wrong thing. |

---

## 4. The correction that matters

An earlier draft of this phase made the Wikisource `WORK` layer
*non-blocking* when the dump published no per-work determination, on the
reasoning that CC BY-SA is an operative grant and what the dump leaves
unsaid could only widen freedom. That took a live seed from 30/30
quarantine to 30/30 accept.

**That reasoning was wrong and has been reverted.**

A transcriber can only license what they own — the keystrokes, the
proofreading, the markup. A Wikisource contributor does not own the work
they transcribed and cannot grant anyone rights in it. CC BY-SA over a
transcription of a text whose copyright status nobody has established
grants nothing usable: if the underlying work is still in copyright, the
composite is unusable no matter what the footer says.

So the `WORK` layer is applicable to the ingested text and unevaluated,
which is precisely what the standing rule names — unknown, absent, or
unevaluated quarantines — and lattice rule 1 blocks it. The re-run gives
**20/20 quarantine, all limited by `WORK`, 0 carrying a determination.**

That is the correct answer, and it means the frwikisource dump currently
yields no acceptable documents. Like DOAB, it stays enabled because it
produces real, rights-assessed candidates for review; it simply produces
none that may be indexed until a per-work determination exists. A
public-domain template on the page is the one thing that fills the layer
from a dump, and none of the twenty sampled pages carried one.

This phase therefore made the system **more** conservative everywhere and
less conservative nowhere.

## 5. Conditions of use

`directory.doabooks.org` publishes, in robots.txt:

```
User-agent: *
Content-Signal: search=yes,ai-train=no,use=reference
Allow: /
```

with a preamble declaring these express reservations of rights under
Article 4 of EU Directive 2019/790. The path rules permit this harvester.
The signal says its content may not be used to train models.

That is a condition on **use**, not on fetching. Obeying it at crawl time
and forgetting it afterwards would honour nothing, so it is parsed from
the same robots.txt the fetcher already reads and recorded on every
document as `no-ai-training` and `use:reference`.

**If Field Horizon's use of this corpus is ever training rather than
retrieval, DOAB content is excluded by that record.**

---

## 6. Rules observed

| Rule | How |
|---|---|
| No general web crawler | No such adapter exists; a test asserts it. |
| Never bypass CAPTCHA / auth / anti-bot | Gallica's altcha refused and declared. DOAB's `/api/search` Cloudflare interstitial not used at all. Standard Ebooks' 401 OPDS still not bypassed. |
| Respect robots.txt and published limits | Enforced in the fetcher; Content-Signal additionally recorded. |
| No legal verdict from an LLM alone | The lattice is deterministic; no model is consulted. |
| An old date is not proof | `INSUFFICIENT_ALONE = {SCAN, OCR, METADATA}`; no date-based inference exists. |
| Unknown licence → quarantine | Rule 1 of the lattice. DOAB/OAPEN: 100% quarantine. |
| No quarantined document in retrieval | `INDEXABLE_STATES` is `{INGESTED, INDEXED}` only. |
| Downloaded content is never instruction | No document content is executed, evaluated, or followed. |
| Never follow a URL from a document body | No such path exists. |
| Secrets via environment only | Three optional tokens, all from env; unit uses `EnvironmentFile`, never `Environment=`. A test asserts it. |
| No 403 worked around | `ACCESS_BLOCKED` with a six-hour retry; no path from a refusal to `DOWNLOADING`. |
| No bulk OCR this phase | Three gates, ceiling of 5, off by default. |
| No global archive run | `archive` never implicit; canary bounds clamp one way only. |
| Timer stays disabled | Not installed, not enabled, not active. No cron entries. |
| Nothing weakened to raise the count | Every rights change in this phase made the system more conservative. The one exception was caught and reverted before acceptance, §4. |

---

## 7. Coverage

```
1288 tests, 0 failures, every outbound socket blocked
```

| Area | Tests |
|---|---|
| Rights lattice | 36 |
| Capability graph | 30 |
| Data roots / isolation | 23 |
| Gallica | 23 |
| Standard Ebooks GitHub | 27 |
| Dumps + Wikisource | 69 |
| DOAB/OAPEN + Content-Signal + broker | 43 |
| OCR | 19 |
| Archive canary | 18 |
| systemd unit | 37 |
| Plan wiring (end to end) | 9 |

---

## 8. The plan now drives acquisition

The first draft of this report named one gap: `build_plan()` existed, was
tested, and nothing called it — the orchestrator decided whether to
download by asking `if candidate.download_url`, in two places. A source
declaring no content role therefore produced download failures instead of
`METADATA_ONLY` records, which is the Phase I behaviour the whole graph
was built to remove, and it was why `gallica_text` shipped disabled.

**That gap is closed.** Planning sits between rights acceptance and
queueing: a document reaches `QUEUED` only once the graph names a
provider for the content role, and otherwise goes straight to the state
that says what is missing. The selection filter one layer up — which
silently discarded every hostless candidate before the pipeline saw it —
is gone; those candidates are now partitioned by plan actionability and
carried outside the item budget they would never spend.

Wiring it also surfaced two `repo.transition(detail=...)` calls that
would have raised `TypeError` the first time a document was parked or
needed OCR. Both sat on paths no test had run end to end.

`gallica_text` is enabled as of this change. See §2.

## 9. What is deliberately not done

- **Gallica content.** Behind an anti-bot challenge. Parsers ready.
- **Europeana.** Needs `$EUROPEANA_API_KEY`; reports
  `READY_WITH_CREDENTIALS`, not "broken".
- **Internet Archive.** Disabled, `trusted_collections` empty, which
  quarantines everything.
- **A provider that hosts what Gallica describes.** The plan will pair
  Gallica's rights evidence with another provider's content the moment
  one is allowlisted, with no code change. None is today, so those
  documents stay in `METADATA_ONLY`.
- **No OCR run.** Deliberate; the brief forbids bulk OCR this phase.
- **Not merged to `main`.**

---

## 10. Verdict

Every step of the brief is implemented, tested, and exercised against the
live services it names, and the acquisition plan now drives the pipeline
rather than sitting beside it.

The system says what it can and cannot do, per provider and per role. Its
most useful outputs in this pilot were the four places it reported
honestly that it could not proceed:

| | |
|---|---|
| Gallica's anti-bot challenge | 6/6 `METADATA_ONLY`, 0 errors |
| DOAB's missing licences | 8/8 quarantine, limited by `WORK` |
| Wikisource's transclusion shells | 176 counted, not dropped |
| Wikisource's undetermined works | 20/20 quarantine, limited by `WORK` |

Phase I would have recorded the first three as failures and the fourth as
an accept.
