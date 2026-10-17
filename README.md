## Quick Start

### Pre-requests

These services run separately from ehclone:

- PostgreSQL with pgvector 0.8.0 or newer (for iterative HNSW scans)
- aria2
- qBittorrent (optional)

### Config

Configure `appdata/config.json`. At least the following keys should be set:

- `db.url`: The database user needs permission to create the database and the
  `vector` extension, or these must already exist.
- `aria2`/`qbit`: download services url and path. When containerized, `remote_dir`
  is what download services sees for destination; `local_dir` is what ehclone sees.
- `eh`: search parameters. Allowed `categories`:
  - `Doujinshi`
  - `Manga`
  - `Artist CG`
  - `Game CG`
  - `Western`
  - `Image Set`
  - `Non-H`
  - `Cosplay`
  - `Asian Porn`
  - `Misc`
- `eh.torrent_key`: optional personal tracker key.
- `archiver.blacklist`: Filter pages by sha1 and decoded qr code.
- `filter.include_tags`: defaults to `[]` (no restriction). Downloads require
  every listed tag. Download may get supressed when the gallery is duped with
  the other gallery without the tag.
- `filter.not_before_min`: Gallery age in minutes before metadata is considered
  stable. Deduplication can run immediately afterward.
  Galleries first fetched after this age do not need a scheduled refresh.
- `filter.dedupe.page_tolerance`; `page_tolerance_rate`; `page_tolerance_ad`:
  Max difference in pages to conrider two gallery dupe. Exculsive.
  The effective tolerance is `max(page_tolerance, ceil(pages * page_tolerance_rate))`,
  add `page_tolerance_ad` when exists `other:extraneous ads`,

After changing deduplication rules, clear `deduped_at` on current winners to rerun
their comparisons; rule changes do not automatically invalidate stored results.

Thumbnail cosine search uses an HNSW index created with the database schema.
`filter.dedupe.candidate_limit` defaults to 128 nearest eligible galleries per
base, after rating, age, winner status, and page-count filters.
HNSW is approximate, and the limit counts candidates rather than confirmed
duplicates, so duplicates may be missed. Increase the limit to consider more
candidates.
> We expect at most 10 candidates match for any gallery, so 128 should be mooooore
> than enough. But if you do find a gallery with more dupes ¯\\(ツ)/¯

## Run

Build the image from the repository root:

```sh
docker build -t ehclone .
```

```sh
docker run --rm \
  -v "$PWD/appdata:/appdata" \
  ehclone
```


## Known Issues

- Sync progress may get inferred by metadata update
    If a gallery becomes expunged between the search response and the metadata reques, it's high GID is stored as
    expunged. The subsequent expunged search starts there

    We sync expunged galleries first so this should be vary rare

- An unavailable gallery can repeatedly block the entire pipeline.
    A page is retried until all galleries succeed. This is a single api call, so this shouldn't happen unless eh is broken.

- Download failures are not automatically retried.
