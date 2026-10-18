# Troubleshooting

Part of the [Pipecat Context Hub README](README.md).

- **Empty results** — run `uvx pipecat-ai-context-hub refresh` to populate the index
- **Stale results** — run `uvx pipecat-ai-context-hub refresh --force` to re-ingest from latest upstream
- **Index corruption** — run `uvx pipecat-ai-context-hub refresh --force --reset-index` to wipe and rebuild.
  Only the data directory is removed: your machine-global
  `~/.config/pipecat-context-hub/config.toml` and any project `.env` are
  preserved, and the reset aborts with an error rather than deleting a
  `PIPECAT_HUB_DATA_DIR` that happens to contain either one — including the
  case where a project `.env` sets `PIPECAT_HUB_DATA_DIR=.`, which would
  otherwise make the working tree holding that `.env` the deletion target.
- **`--prune` / `PIPECAT_HUB_PRUNE`** — `refresh` no longer deletes
  previously-indexed data for a repo that isn't configured *for this
  invocation* by default; it only warns
  (`Repo <slug> not configured in this run; leaving N indexed record(s) in
  place — pass --prune to remove`) and leaves the records and metadata in
  place, since the repo may still be configured elsewhere (e.g. in
  `config.toml` but shadowed by a narrower project `.env`). Pass `--prune`,
  or set `PIPECAT_HUB_PRUNE=1`, to actually delete that data. This var is
  invocation-scoped: it is not read from `config.toml` (see [Config
  precedence](configuration.md#config-precedence-env-vars-env-and-configtoml)) and is
  intentionally excluded from `config.toml.example` and the Environment
  Variables table above. Tainted repos
  (`PIPECAT_HUB_TAINTED_REPOS`) are unaffected — they are always cleaned up,
  with or without `--prune`.
- **`refresh` refuses to start: low disk, or another `refresh` running** —
  `refresh` first checks free space on the data-dir volume (default 5 GB;
  `PIPECAT_HUB_MIN_FREE_GB=0` disables, other non-negative numbers set the
  threshold in GB, and an unparsable, negative, or non-finite value, or one
  too large to convert to bytes (e.g. `1e300`), silently falls back to the
  5 GB default; with
  `--reset-index` the check runs after the delete has freed the space.
  `PIPECAT_HUB_MIN_FREE_GB` is invocation-scoped like `PIPECAT_HUB_PRUNE`: set
  it in the environment, not `config.toml`, which is why it is not in the
  Environment Variables table in `configuration.md`; below it, `refresh` exits **1**) and
  takes an exclusive `refresh.lock` (a sibling file of the data dir,
  `<dirname>.refresh.lock`, e.g. `~/.pipecat-context-hub.refresh.lock`, so `--reset-index` can't delete
  it), so two refreshes can't interleave writes to the same Chroma HNSW files.
  Locking is best-effort: if the lock file is a symlink, not a regular file,
  has other hard links, or belongs to another user, it is not used and the
  command logs `Lock file … continuing without a lock` and proceeds unlocked
  (as it does where file locking is unsupported). On Windows, which can't open
  without following symlinks, a link swapped in during the open is refused only
  after the open, which can already have created an empty file at its target
  (and the start-of-refresh timestamp bump goes by path); doing that needs
  symlink privilege and write access to the data dir's parent, and is accepted.
  The hub never deletes or replaces a lock file; if something outside it does
  while the lock is held, the next process locks the new file and two refreshes
  can overlap (accepted on every platform). `--reset-index` exits **1** without
  rebuilding when any index file (keyword database, vector directory or
  deprecation map) survives its delete (on Windows, a running
  `serve` holds them open: stop it, then retry).
  **While a refresh runs, nothing reads the index:** `serve` answers every tool
  call with an error saying "retry in 60-120 seconds" (a `serve` started
  mid-refresh still connects, with the index unopened), and one-shot CLI
  queries exit **75** (`EX_TEMPFAIL`) with the same message on stderr. Readers
  open the index under a shared hold on `refresh.lock`, so a refresh waits (up
  to ~10 s) for an open in progress rather than starting under it (if readers
  still hold it, `refresh` says so: just retry it). A `refresh` refused by the
  lock (another refresh, or readers outlasting the wait) also exits **75**. The
  dashboard scripts (`just dashboard-build`) open the index the same way, so
  they too refuse to start while a refresh runs, and on a missing or empty
  index they exit with the `refresh` hint instead of creating one. Readers
  never create the index: if its vector collection has gone missing or empty
  beside a populated keyword database, queries say to run `refresh --force`
  rather than returning empty results, and a plain `refresh` that finds the
  vector collection lost that way exits **2** with the same advice, without
  touching it (it would otherwise skip every unchanged source and leave vector
  search empty). The reverse, a keyword database missing, incomplete or empty
  beside a vector collection that still has records, makes queries and every
  `refresh` (`--force` included) exit **2** saying to run
  `refresh --force --reset-index`, without touching the vector collection: the
  keyword database is where `refresh` records which repos it indexed, so
  without it the vectors of a repo no longer configured could never be
  replaced or pruned. (An interrupted `refresh` never leaves this state:
  keyword rows are written before their vectors, so a first `refresh` stopped
  mid-write leaves keyword records ahead of the vectors, and
  `refresh --force` rebuilds it.) A missing keyword database beside an empty or absent vector
  collection is simply rebuilt by `refresh`. A corrupt keyword
  database (or one whose schema it cannot use) also makes `refresh` exit **2**,
  saying to run `refresh --force --reset-index`; a locked or busy one does not.
  `refresh --force` recreates the lost collection, and whenever `refresh` finds
  the vector collection empty it stops trusting every source's unchanged-skip
  marker (it drops the docs content hash and flags each indexed repo
  `repo:<slug>:vector_empty`, separate from `cleanup_failed`; repos stay known
  to the cleanup and taint passes), so a source whose
  fetch fails in that run is re-ingested by the next plain `refresh` rather than
  left keyword-only. It re-ingests only the sources in its configuration: a repo indexed earlier but
  absent from this invocation's config keeps its keyword records but gets no
  vectors back, so re-add it to the config for that run, or pass `--prune` to
  drop it. `serve`
  reopens the rebuilt index (with the same readiness checks as at startup) and
  reloads the deprecation map by itself once the refresh ends, so no MCP client
  restart is needed; a call that lands while an earlier call still uses the old
  index is asked to retry in a few seconds. After a `--force` refresh the vector graph is rebuilt from
  the live records (delete + re-add otherwise left dead elements that grew the
  graph by the whole corpus each time; the swap never leaves the index without
  its collection, and one interrupted by a crash is restored by the next
  `refresh` — until then queries say to run it; if the swap fails leaving no
  collection, that `refresh` exits 2 without deleting anything and the next
  `refresh` restores it; if that restore fails too, it exits 2 and says to
  rebuild with `refresh --force --reset-index`). After each upsert batch the vector index re-checks the persisted HNSW graph
  and aborts with `corrupt persisted HNSW` if `link_lists.bin` outgrows its
  header-derived bound — the runaway-file corruption that once took ~325 GB
  of a disk. Recover with `refresh --force --reset-index`.
- **`serve` exits immediately with code 2** — the index is empty or
  unopenable. Run `uvx pipecat-ai-context-hub refresh` (or
  `refresh --force --reset-index` if the error message mentions a failed
  open) and try again. This is deliberate: prior versions started anyway
  and MCP clients hung on every query.
- **Stale `serve` processes** — `serve` polls its parent PID every 2s
  and exits cleanly when the MCP client disappears (look for
  `Shutting down: parent_died original_ppid=… current_ppid=1` in the
  trace). Under `uv run` (where the immediate parent is `uv`, not the
  client), it instead watches the grandparent and logs
  `Shutting down: client_died client_pid=…`. If you still see orphans
  (older versions, an unrecognized lingering launcher, or Windows where
  the watchdog is disabled), `pkill -f "pipecat-context-hub serve"` is
  safe to run between sessions.
- **`Idle watchdog disabled: watching … for client exit` on boot** —
  this `INFO` line is expected and benign. It means `serve` has reliable
  client-death detection (direct parent, or `uv`/`uvx`/`poetry`/… with a
  resolved grandparent), so the idle timeout is switched off to keep the
  hub warm through quiet stretches of an active session. No action
  needed; set `PIPECAT_HUB_IDLE_TIMEOUT_SECS` if you want an idle
  backstop anyway.
- **Diagnosing degraded starts** — on `serve` boot, look for
  `pipecat-context-hub vX.Y.Z starting: …` (`INFO`) to confirm the running
  version and index content-type counts. If reranking is off, a
  `Reranker disabled at startup: reason=…` (`WARNING`) line names the
  cause (`config_disabled` | `not_cached`) and, for `not_cached`, the
  exact HF cache directory probed.

- **Where to report what** — you shouldn't usually need to look this up: both
  front doors surface it themselves. The one-shot CLI prints a remediation-first
  hint on stderr (never on stdout, so piped JSON stays clean) whenever a
  semantic command returns poor/empty results or the reranker model is
  uncached (the poor-results hint is skipped when the response carries a
  `corpus_hint`, which names the missing repos instead); `serve`'s MCP
  `initialize` response gives a connecting agent the
  same guidance. If you're filing directly: poor or missing search results go
  to [retrieval-quality.yml](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml)
  (the template includes a diagnostic prompt your coding agent can run to
  generate a structured report); crashes, incorrect behavior, or anything else
  go to [bug-report.yml](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=bug-report.yml).
- **A search response carries `corpus_hint`** — the query names a vendor or
  topic whose catalog repos are not indexed, so the results cannot cover it.
  Run the hint's `add_command` (it only adds them to `config.toml`), then
  `pipecat-context-hub refresh`; restart `serve` afterwards, since it reads
  configured repos once at start. The hint carries `refresh_command` only when
  `repos_configured_no_records` is non-empty: those repos are already
  configured but have no records, so run `refresh` and check its log for a
  failed clone.

## Windows

- **Index commands fail closed** — populated `refresh`, `serve`, and semantic
  queries require POSIX descriptor-relative filesystem operations. The Windows
  CI job proves package installation and path/ingest helpers only; it does not
  provide a supported populated index. Run the hub on a POSIX host instead of
  deleting the Windows cache and retrying `refresh`.
- Windows does not produce a populated refresh summary: populated index
  operations fail closed before the refresh crawl. If a summary from an older
  POSIX run is being displayed through a Windows console, set
  `PYTHONIOENCODING=utf-8` or use Windows Terminal for Unicode output; run the
  actual refresh on a supported POSIX host.

## Can't see which config a `serve` session actually used

`serve` logs `cwd=… env_keys=…` at `INFO` on every boot (home-redacted, key
names only), which tells you which directory — and therefore which `.env`, if
any — shadowed `config.toml` for that session. If your MCP client hides the
server's stderr entirely, set `PIPECAT_HUB_DEBUG_PROBE=1` in the client's
`env` block: `serve` appends the same evidence to
`~/.cache/pipecat-context-hub/serve-debug.log` on each boot. This is a
troubleshooting fallback, not a setting — it is deliberately absent from
`config.toml.example` and the `configuration.md` Environment Variables table, and is ignored when
set in `config.toml` (a machine-global file must never persistently enable a
disk-writing probe). Unset it once you've read the file.

## Pointing the hub at a different `config.toml`

`PIPECAT_HUB_CONFIG_FILE=/path/to/config.toml` overrides the lookup path for
the machine-global config file — useful for testing a config before installing
it, or for running one invocation against a different profile. Like
`PIPECAT_HUB_DEBUG_PROBE` above, it is invocation-scoped rather than a setting:
it is deliberately absent from `config.toml.example` and the Environment
Variables table, and is ignored when set *inside* `config.toml` (honouring it
from within the file it locates would be circular). A `~`-rooted value is
expanded; a value that isn't a usable path is ignored with a warning and **no**
config file is loaded — the hub will not quietly fall back to the default
location when you asked for a specific file. Setting it to an *empty* value
(`PIPECAT_HUB_CONFIG_FILE=`) is treated the same way: it disables the lookup
for that invocation (warning, no config loaded). Unset the variable entirely
to go back to the default location. `pipecat-context-hub repos add|remove|list`
honour it too: they edit and read the file it names, and their refusals name that
path.
