# Corpus Dump Operations

Reading a whole wiki through `action=query` means hundreds of thousands
of requests against an institution that has already packaged the entire
thing into one file and asked people to use that instead. This document
covers the dump route: how a release is resolved, how a multi-gigabyte
file is read without downloading it, and what to do when a run is
interrupted.

## The bounded-cost design

`frwikisource-pages-articles-multistream.xml.bz2` is **2.8 GB**.
`dump-seed` mode wants 20–50 works. Downloading the file to read fifty
pages would be absurd and rude.

A multistream dump is a concatenation of independently decompressible
bzip2 streams of 100 pages each, and a companion index maps page titles
to byte offsets. So:

```
1. GET /frwikisource/                     the dated releases
2. GET /frwikisource/20260701/dumpstatus.json    the manifest, with SHA-1s
3. GET the index, Range: bytes=0-524287   a 512 kB prefix
4. GET the data,  Range: bytes=N-M        one ~45 kB stream per 100 pages
```

Measured live, 2026-08-01:

```
30 works, 9 requests, 875,157 bytes of 2,801,978,811 (0.031%), 31s
```

The index deserves its own note: it is 28 MB compressed and expands past
**200 MB**. Buffering all of it to take thirty titles is the same mistake
as downloading the dump to read fifty pages, one layer down. Seed mode
reads a 512 kB prefix — the first 300 kB of the real index held 29,126
entries across 292 streams. Archive mode reads it whole, because it
genuinely needs every offset.

## Resolving a release

**`latest/` is never used.** It is a symlink whose target moves, and a
run that resolves it twice — once for the index, once for the data — can
read one release's index against the next release's data, which puts byte
offsets in the middle of the wrong stream. Only dated directories.

Only jobs the service reports as `done` contribute files. A job that is
`waiting` or `in-progress` has files that either do not exist or are
still being written, and reading one gives a truncated corpus with no
error anywhere.

An **ambiguous file match raises**. The sharded jobs publish names
differing only by a page range, and quietly taking shard 1 of 8 because
it sorted first looks exactly like a successful run that lost seven
eighths of the corpus.

Pin a release for reproducibility:

```yaml
options:
  dump_date: "20260701"     # exactly this release
  not_after: "20260701"     # the newest release at or before this date
```

## Downloading

Resumable and verified:

- Bytes land in `<name>.part`. A complete file and a half-written one are
  never the same path, so nothing downstream can read a truncated dump
  believing it whole.
- Resumption uses HTTP `Range`. A 20 GB download interrupted at 19 GB
  resumes at 19 GB.
- A `.part` **longer** than the manifest declares is discarded, not
  truncated: its tail belongs to a different release, which the checksum
  would catch only after a multi-gigabyte download.
- SHA-1 is verified **before** the rename, so a file that fails its
  checksum never appears under its real name even for an instant. A
  mismatched part is deleted rather than resumed — whatever is wrong with
  it is in bytes already written.
- Hashing is streamed. `path.read_bytes()` on a multi-gigabyte file is
  how a harvester gets killed by the OOM reaper at the last step of a
  long download.

## Range requests

The fetcher refuses a `200` answering a `Range` request. Without that
check, a server ignoring `Range` would send the whole 2.8 GB file, the
size cap would return its first N bytes, and the caller would receive a
truncated body next to a success status — indistinguishable from the
45 kB stream it asked for.

## Checkpoints

A checkpoint is `offset:page_id` — the stream's byte offset plus the last
page processed inside it. Both are needed: the offset alone restarts a
stream and re-imports up to 99 documents; the page id alone does not say
which stream to seek to.

Page ids are meaningful only *within* their stream. They are not ordered
across the dump, so a later stream is always read in full even if its ids
are lower.

An unreadable checkpoint restarts that dump cleanly rather than crashing.
It should cost one run's progress, not the run.

## Namespace filtering

**The `<ns>` element is the authority, never the title.** A
main-namespace title may contain a colon — the live French index holds:

```
297482:1881:Les Fougerêts : Patrimoine et identité d’une commune de Haute-Bretagne
```

Splitting that on `:` files a real book under a namespace called
`Les Fougerêts ` and truncates its title.

Redirects are excluded too. A redirect page has a real title, a real id,
and a body of `#REDIRECT [[…]]`; it imports perfectly cleanly as a
nine-word document and pollutes the deduplicator with a title that
already exists. The live sample was 13 redirects in its first 14 pages.

## What the dump does not contain

Wikisource keeps the transcribed text of most works in the `Page:`
namespace and assembles it at render time. `pages-articles` carries the
main namespace, where those works are `<pages index="…djvu"/>` and
nothing else — complete, correct pages containing no text. In a live
seed, **176 of 206** pages were such shells.

They are counted and reported. An operator seeing "30 works" deserves to
know the other 176 were the `Page:` namespace and not a broken parser.

One consequence worth knowing: **the subpage rule inverts on this
route.** `wikimedia.is_importable_page` skips subpages because on the API
route the parent's *rendered* output already contains every chapter. A
dump has no rendered output — the parent is the bare directive and the
subpage holds the prose. Applying the API rule to a dump imports exactly
nothing.

## Transports

```yaml
options:
  transport: xml_export     # dumps.wikimedia.org, no credentials, the default
  # transport: enterprise   # needs $WIKIMEDIA_ENTERPRISE_TOKEN
```

The public export is the normal path, not a degraded one. Without a token
the Enterprise transport refuses to run and says which variable to set;
it never silently falls back.

## Archive mode

`archive` is never entered implicitly and requires an explicit storage
budget. **Run `archive-canary` first** — it is the same machinery with
every dial turned down:

```yaml
archive_canary:
  max_items: 1000
  max_download_bytes: 21474836480          # 20 GB
  min_free_disk_bytes: 53687091200         # 50 GB
  max_concurrency: 1
  checkpoint_every_items: 25
  stop_on_rights_violation: true
```

The bounds clamp one way. A config file may tighten any of them and may
not loosen one; `min_free_disk_bytes` clamps the other way because it is
a floor. `stop_on_rights_violation` cannot be switched off. A canary a
yaml edit can turn into an unbounded archive run is not a canary.

Concurrency stays at 1. It is the fastest way to turn a polite harvester
into a denial-of-service against an institution giving its files away.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "no completed job published any file" | The release is still being generated. Pick an earlier date. |
| "N files match" | Ambiguous match; name the file more precisely. |
| "the first N bytes of the index yielded no entries" | The prefix is too small, or the file is not the index. |
| "asked for bytes=… and got 200" | The server does not honour `Range` on that resource. |
| "sha1 … does not match" | The part is corrupt and has been deleted. Re-run. |
| 0 works, many shells skipped | Normal for Wikisource. The text is in the `Page:` namespace. |

## Related

- [CORPUS_SOURCE_GRAPH.md](CORPUS_SOURCE_GRAPH.md) — roles and states
- [CORPUS_OPERATIONS.md](CORPUS_OPERATIONS.md) — day-to-day running
