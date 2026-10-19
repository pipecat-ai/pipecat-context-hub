# Index Metadata Contract

Part of the [Pipecat Context Hub README](README.md).

External tooling — editor plugins, CI checks, the Pipecat CLI — often needs one
cheap answer: *how old is this index, and which pipecat version is it for?*
Lookup-only paths such as `status` and `check-deprecation` avoid constructing
the native Chroma client, but still validate the persisted Chroma SQLite file
and HNSW safety metadata; semantic paths additionally open ChromaDB. Importing the hub
or shelling out to `status` is still too slow to run on every invocation of
another tool, so the direct metadata contract remains useful.

The `index_metadata` table in `<data dir>/metadata.db` is therefore a **published
read contract**. Read it directly, read-only, with the standard library:

```python
import os, sqlite3
from pathlib import Path

data_dir = Path(os.environ.get("PIPECAT_HUB_DATA_DIR") or Path.home() / ".pipecat-context-hub")
conn = sqlite3.connect(f"file:{data_dir / 'metadata.db'}?mode=ro", uri=True, timeout=0)
meta = dict(conn.execute("SELECT key, value FROM index_metadata"))
```

The database is WAL, so a reader neither blocks nor is blocked by a concurrent
`refresh` or a running `serve`, and sees only committed state. Use `mode=ro` and
`timeout=0`, and treat any exception as "unknown" — a freshness check must never
fail its caller's command.

Consumers **must** honour `PIPECAT_HUB_DATA_DIR`; assuming
`~/.pipecat-context-hub` will report "no index" at anyone who relocated theirs.

All contract keys written at the end of a single refresh (contract version,
timestamps, upsert/error counts, `framework_version`, and the
`indexed_framework_version`/`indexed_framework_commits_ahead` pair) are written
in one transaction, so a reader never observes a partially-updated related-key
set — e.g. a new `indexed_framework_version` can never be paired with a stale
`indexed_framework_commits_ahead` from a previous refresh.

## Contracted keys

| Key | Meaning |
|-----|---------|
| `metadata_contract_version` | Version of this contract (currently `3`). Absent on indexes built before it was published |
| `last_refresh_at` | UTC ISO-8601 timestamp of the last completed refresh |
| `last_refresh_error_count` | Errors in that refresh; `last_refresh_errored_at` is present only when non-zero |
| `indexed_framework_version` | Nearest pipecat release tag the index was built from, e.g. `1.6.0` |
| `indexed_framework_commits_ahead` | Commits from that tag to the indexed revision. `0` means the index *is* that release |
| `deprecation_map_commit_sha` | Pipecat commit SHA the on-disk deprecation map was built from. Cross-checked against `indexed_framework_version`'s stamp by `resolve_framework_version` — a mismatch means the map and the stamp were published in different runs (crash between the two writes), so the exact-version answer falls back to `None`/intrinsic status |
| `framework_version` | The pin behind the indexed framework records, recorded verbatim — so a `latest` pin stores `latest`, not the tag it resolved to, and the default-branch sentinel stores `head`. Present whenever the index holds framework records, since an unspecified pin defaults to `latest` rather than to no pin — this is *not* the version the index was built from |
| `repo:<org>/<repo>:commit_sha` | Indexed commit for each source repo |
| `content_type_counts` | JSON object of record counts by content type |

`indexed_framework_version` is a **floor, not an identity**: a `head` refresh
tracks the default branch, so an index built 55 commits past `v1.6.0` still
reports `1.6.0` with `indexed_framework_commits_ahead: 55`. Compare versions with
that slack in mind, or every developer working from a source checkout gets a
spurious mismatch.

`indexed_framework_commits_ahead` is `0` **if and only if**
`indexed_framework_version` is a parseable release the checkout sits exactly on.
A pin that is not a release — `head`, or a branch-shaped or feature tag — is
recorded under `framework_version` but falls back to git-describe floor
semantics, so the pair never claims exactness for a value that is not a version.

`check_deprecation` uses `indexed_framework_version` as its default `version`
only when `indexed_framework_commits_ahead` is `0`. For a floor — a `head`
refresh, or an index built before this contract — it evaluates symbols at their
intrinsic registry status instead, rather than answering as though the index were
the older release the floor names.

If the deprecation map is missing or unreadable, `check_deprecation` returns a
null `status` and a note to run `refresh` rather than answering `current` for
every symbol, and `serve` logs a warning at startup.

## Compatibility

`metadata_contract_version` is bumped only when the table's shape or a documented
key's meaning changes; adding a key is backwards compatible and does not bump it.
Treat a value **higher** than you understand as "unknown" and stay silent rather
than guessing.

The keys are written on refresh, so an index built by an older hub simply lacks
the newer ones. `last_refresh_at` and `repo:*:commit_sha` predate the contract
and are present on essentially every index in the wild, so a staleness check
works immediately; version comparison starts working after the next refresh.
