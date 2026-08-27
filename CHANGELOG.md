# Changelog

All notable changes to the Pipecat Context Hub are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **`install` registers with Claude Code for every directory.** A fresh
  registration is made at Claude's `user` scope rather than its default `local`,
  which keys the entry on the directory `install` happened to run in and leaves
  every other project without the server — silently, since an agent without the
  tools answers from training data instead of failing. Nothing about the hub is
  per-project: one index serves the machine, and the registered command is an
  absolute path to a global install. An entry that already exists is still
  repaired at whatever scope it holds, so a deliberate `local` or `project`
  registration is left where it is. Codex is unaffected; its single config is
  already machine-wide.

  Existing per-directory registrations keep working and are not migrated. To
  move one, remove it and re-run `install`:

  ```
  claude mcp remove pipecat-context-hub -s local
  pipecat-context-hub install --no-refresh
  ```

## [0.5.3] - 2026-08-19

### Added
- **`--framework-version latest`.** `refresh` accepts `latest` (case-insensitive)
  wherever a framework tag is expected — CLI flag or
  `PIPECAT_HUB_FRAMEWORK_VERSION` — and resolves it to the highest release tag on
  `pipecat-ai/pipecat`. Without a pin, refresh still indexes the default branch
  (`main`); `latest` is the way to index a released version without hard-coding a
  tag that goes stale. It re-resolves on every run, so setting it in an MCP
  client's `env` block tracks releases as they ship and a plain incremental
  `refresh` picks up a new release without `--force`. Resolution is version-aware
  rather than lexicographic (`v1.10.0` beats `v1.7.0`), skips prereleases unless
  the repo has nothing else, and ignores tags that do not parse as versions. The
  pin is still framework-only, and `framework_version` index metadata records
  `latest` verbatim — `indexed_framework_version` continues to report the concrete
  tag the index was built from.

### Changed
- **`check_deprecation` no longer answers at a floor version.** Its default
  `version` (used when the caller passes none) is now `indexed_framework_version`
  only when `indexed_framework_commits_ahead` is `0` — i.e. when the index sits
  exactly on that release. For a floor — an unpinned default-branch refresh, or
  an index built before the provenance keys existed — it evaluates symbols at
  their intrinsic registry status instead. Previously an index built 80 commits
  past `v1.5.0` answered as if it were `1.5.0`, reporting symbols deprecated in
  `1.6.0` as "current". Existing indexes get the new behaviour without a
  re-index; pass `version` explicitly to pin the evaluation yourself.

### Fixed
- **Deprecation map and framework provenance stranded a revision behind after a
  partial framework ingest.** `refresh` rebuilt `deprecation_map.json` and
  restamped `indexed_framework_version` only when the framework repo ingested
  without a single error — but the stale records are deleted *before* ingest, so
  one unreadable file left the index holding the new checkout's code while the
  map and the stamp still described the previous revision. Both now follow record
  *replacement* (whether `delete_by_repo` ran), which is the condition that
  actually decides which revision the index describes. A framework repo whose
  checkout failed never reaches the delete, so its map and stamp are still
  preserved.
- **A repo whose ingest errored after old records were already deleted could be
  left with a silent mix of stale-empty and partial-new records.** The partial
  new records are now purged too, so an errored repo ends the run with zero
  records and a warning naming the repo and prompting a retry, rather than a
  misleading partial index.
- **`latest` skipped an uppercase-`V` release tag.** The release-tag parse
  stripped a lowercase `v` but rejected any leading `v` *or* `V` left behind, so
  a well-formed `V2.0.0` was classed unparseable — a repo whose newest release
  used that spelling silently resolved `latest` to an older `v1.x`, and such tags
  sorted as junk in the "available tags" error hint. The strip is now symmetric
  with the guard; a genuinely doubled prefix (`vv`/`vV`/`Vv`/`VV`) is still
  rejected.
- **One bad tag anywhere in the repo aborted `latest`.** Resolution validated
  every version-like tag in the repository — resolving each one's commit and
  checking every version group for ambiguous aliases — before selecting
  anything, so a single unresolvable ref or one historical ambiguous alias pair
  failed the whole resolution even when neither tag was remotely the newest.
  Validation now applies to the selected tag only; it still fails closed on that
  one.
- **A non-release `--framework-version` pin was stamped as an exact version.**
  A branch-shaped or feature tag (which git and the tool's tag validation both
  accept) was written verbatim to `indexed_framework_version` with
  `indexed_framework_commits_ahead: "0"`, publishing a non-version against a
  contract that promises one and breaking downstream version comparisons —
  including `check_deprecation`, which silently degraded. Such a pin now falls
  back to git-describe floor semantics for both the metadata stamp and the
  per-chunk version pin.
- **`latest` could be stamped with a tag the indexed commit is not at.** The
  resolved tag was recovered by re-running the resolution against the clone
  *after* the checkout, without the origin verification the first resolution
  performed — so a concurrent refresh landing a newer tag in between could name
  a release the indexed commit does not correspond to. The verified tag is now
  returned by the clone itself.
- **Version-ordered tag hint on an unresolvable `--framework-version`.** The
  "available tags (latest 5)" list in the error sorted tag names as strings, so it
  would have ranked `v1.7.0` above `v1.10.0` and omitted the newest release from
  the hint once pipecat reaches a double-digit minor.
- **Reranker permanently disabled after a successful `refresh`.** `is_model_cached`
  probed the HF cache at `revision="main"` while `_download` fetches shipped
  models at a pinned commit SHA — a pinned download populates
  `snapshots/<sha>/` but never writes `refs/main`, so the probe always missed
  it and reported `reranker_disabled_reason: "not_cached"` even with a
  complete on-disk model. Re-running `refresh` could not fix it, since the
  SHA was already cached and no name-based resolve occurred to write the
  missing ref. The probe now checks the same pinned revision the download
  used. (#115)
- **`--prune-tags` could prune a pinned `--framework-version` tag out from under
  itself.** A pinned fetch that pruned remote tags could remove the pinned tag
  if it had disappeared upstream between runs, leaving the pin unresolvable.
  `--prune-tags` now only runs when resolving `latest`, not for a literal pin.
- **A tainted ref no longer resolvable locally fell through as untainted.** A
  ref that was deleted or pruned upstream after being marked tainted previously
  fell through the taint check as if it were clean; it's now treated as
  tainted, matching the function's other failure branches.
- **A stale exact-provenance stamp could survive a refresh that couldn't derive
  a new one.** When a refresh replaced the framework repo's records but the
  resulting checkout was branch-shaped (not a release), `indexed_framework_version`
  and `indexed_framework_commits_ahead` were left holding the previous run's
  stamp instead of being cleared, so a stale exact version kept describing
  records it no longer matched.
- **`IndexStore` write/delete methods now re-raise FTS-layer failures** —
  `upsert`, `delete_by_content_type`, `delete_by_repo`, and `delete_by_source`
  previously swallowed a SQLite FTS5-side error, leaving the vector and FTS
  indexes silently diverged. `refresh`'s call sites handle the re-raise per
  context: the pre-ingest per-repo delete records an error and skips that
  repo, while cleanup-style deletes (post-error purge, tainted-ref cleanup,
  docs cleanup, unconfigured-repo removal) log the failure and continue on a
  best-effort basis rather than aborting an otherwise-successful refresh.
- **A failed tainted-ref purge could wipe its own retry bookkeeping.** When a
  repo's *indexed* ref was also tainted, a failed `delete_by_repo` was logged
  and swallowed but execution still fell through to clearing the repo's
  commit-SHA metadata (and, for the framework repo, both provenance keys) —
  so a future refresh had no trail that stale tainted records might still be
  sitting in the index. That metadata now only clears once the purge actually
  succeeds.
- **Cleanup-pass delete failures were logged but never counted as errors.**
  All three `delete_by_repo` cleanup sites in `refresh` (tainted-repo removal,
  `--prune`-authorized removal of an unconfigured repo, and tainted-ref
  cleanup — now unified behind `_delete_repo_index_data`) append a message to
  `all_errors` on failure, so a failed purge surfaces through the refresh
  summary's error count instead of only a log line.
- **A failed stale-docs delete could skip re-ingestion forever.** When
  `delete_by_content_type("doc")` failed, `docs:content_hash` still described
  the (partially-diverged) old crawl, so an unforced refresh kept taking the
  "docs unchanged, skip" shortcut on every subsequent run. `docs:content_hash`
  is now cleared as part of the failure handler, so the next refresh — forced
  or not — always retries the delete and re-ingest instead of silently
  skipping.
- **A corrupt or unreadable deprecation registry silently published an empty
  deprecation map.** `build_deprecation_map_from_registry` distinguished a
  legitimately absent registry (older pipecat versions) from a present but
  unparseable one only by both returning an empty map — so a truncated/corrupt
  `deprecations.json` overwrote a previously good `deprecation_map.json` with
  nothing, and every deprecation check against it went silently blank. A
  present-but-unreadable registry now raises `DeprecationRegistryError`, and
  `refresh` catches it to preserve the existing on-disk map instead of
  publishing an empty one.
- **A schema-invalid (but syntactically valid) deprecation registry bypassed
  `DeprecationRegistryError` entirely.** A registry root with the
  `deprecations` key simply absent is a legitimate empty map, but a bare list
  root, or a present `deprecations` field that isn't a list (e.g. `null`),
  either produced a silent empty map or an unhandled `TypeError`/
  `AttributeError` that aborted the whole refresh. Both invalid shapes now
  raise `DeprecationRegistryError` up front, so `refresh` preserves the
  existing map the same way it does for unreadable JSON.
- **The framework provenance stamp could advance independently of the
  deprecation map it should describe.** `indexed_framework_version` /
  `indexed_framework_commits_ahead` were gated on the framework records having
  been replaced, but not on the deprecation map having actually been
  published — so a `dep_map.save()` failure (or a registry error) could leave
  `deprecation_map.json` describing the old checkout while the stamp advanced
  to describe the new one, producing an incoherent revision triple. The stamp
  is now gated on the map publish having succeeded, so the two always move
  together.
- **A repo whose stale-record cleanup failed could be skipped forever with an
  empty vector index.** `refresh`'s unchanged-SHA skip shortcut trusted
  `indexed_records > 0` (an FTS-side count) as proof a repo's index is
  healthy — but a failed `_delete_repo_index_data` call (vector delete
  succeeds, FTS delete raises) can leave stale FTS rows behind even though the
  vector store is now empty for that repo, letting the shortcut fire
  indefinitely on a silently-empty index. Cleanup failures now set a
  `cleanup_failed` flag that blocks the shortcut until the repo is
  successfully re-ingested.
- **The docs-recovery hash-clearing call (added to fix the "skipped forever"
  bug above) was itself unprotected.** If the same underlying storage fault
  that broke `delete_by_content_type` also broke `delete_metadata`, the
  exception propagated unhandled and aborted the whole refresh — reproducing
  the permanent-skip bug one layer deeper. That call is now wrapped in its own
  try/except, best-effort, with the failure recorded in the refresh summary.
- **`--framework-version` pins with an uppercase-`V` prefix could fail to
  resolve.** `_resolve_tag`'s candidate generation only checked
  `tag.startswith("v")`, so a literal pin like `V1.2.0` never tried the
  un-prefixed tag as a fallback candidate even though `latest` resolution and
  version parsing both already handle that case. Candidate generation is now
  case-insensitive, matching the rest of the pin-resolution path.
- **`--framework-version` pins with an uppercase-`V` prefix could still fail
  to resolve when only the lowercase-prefixed tag exists upstream (the common
  case).** The previous fix made candidate generation case-insensitive but
  still branched on whether the input already had a `v`/`V` prefix, so an
  already-prefixed pin like `V1.2.0` produced candidates `["V1.2.0", "1.2.0"]`
  and never tried the real upstream tag `v1.2.0`. Candidate generation now
  always includes the bare, lowercase-`v`-prefixed, and as-given forms.
- **`deprecation_map.json` could be left truncated or invalid by an
  interrupted write.** `DeprecationMap.save()` wrote directly to the target
  path, so a crash, disk-full, or other interruption mid-write left partial
  JSON in place; `load()` silently treats invalid JSON as an empty map,
  destroying the last known-good deprecation map. `save()` now writes to a
  temp file and atomically renames it into place, so `path` always holds
  either the complete prior map or the complete new one.
- **`refresh`'s deprecation-map build/save step only tolerated
  `DeprecationRegistryError`/`OSError`.** Any other exception from
  `build_deprecation_map_from_registry` or `dep_map.save()` propagated
  uncaught and aborted the whole refresh instead of being recorded as a
  refresh error with the existing map preserved. The catch is now
  exception-agnostic, matching the message-formatting logic below it.
- **The deprecation map and its `indexed_framework_version` provenance stamp
  could silently describe different revisions after a crash between the two
  writes.** `check_deprecation`'s default-version resolution had no way to
  detect that divergence and could assert version-exactness against a map
  that no longer matched the stamped revision. A refresh now also stamps
  `deprecation_map_commit_sha` alongside the other provenance metadata, and
  `resolve_framework_version` cross-checks it against the loaded map's own
  commit SHA, falling back to `None` (intrinsic registry status) on a
  mismatch. Missing/older indexes without the new stamp are unaffected — the
  check is skipped when either side is absent.
- **`_delete_repo_index_data`'s bookkeeping `delete_metadata` calls (run after
  a successful record delete) were unguarded.** An exception there propagated
  uncaught instead of being recorded through the same failure path as the
  record-delete step. Those calls are now wrapped in their own try/except,
  returning `False` and setting the repo's `cleanup_failed` marker on
  failure, consistent with the rest of the helper.
- **A stale exact-provenance stamp could survive a refresh that replaced the
  framework repo's records but then failed to publish the deprecation map.**
  `indexed_framework_version` / `indexed_framework_commits_ahead` /
  `deprecation_map_commit_sha` were left describing the previous revision —
  still consistent with the untouched on-disk map, but no longer with the
  records the index actually holds, a divergence the existing map/stamp
  SHA cross-check can't detect since both sides still match each other. The
  stamp is now cleared whenever the framework repo's records were replaced
  this run but the map publish did not follow, so `resolve_framework_version`
  falls back to intrinsic status instead of a wrong exact answer. A refresh
  where the framework repo simply didn't change is unaffected — its old
  stamp is still accurate and is left alone.
- **The pre-ingest `delete_by_repo` failure branch didn't record the
  `cleanup_failed` marker** that the sibling `_delete_repo_index_data`
  failure paths already set. Defense-in-depth / consistency fix — this
  branch's existing `continue` already skips `ingested_repos.add()`, and the
  SHA-bookkeeping loop already deletes the repo's stored `commit_sha` on any
  failure path, so a same-SHA skip was never actually reachable here; the
  marker is now set anyway for parity with the rest of the cleanup-failure
  handling.
- **`_delete_repo_index_data`'s `delete_by_repo`-failure except block called
  `set_metadata` for the `cleanup_failed` marker unguarded**, unlike the
  sibling bookkeeping-deletes except block fixed previously. A failure in
  both `delete_by_repo` and that follow-up `set_metadata` call would have
  propagated uncaught instead of returning `False`. Now wrapped in its own
  try/except, mirroring the sibling pattern exactly.
- **A malformed `removals.json` was silently treated as empty instead of
  raising.** `add_removals_from_registry` swallowed every non-
  `FileNotFoundError` read/parse failure, treated a non-dict JSON root as
  legitimately empty, and never validated a present `removals` field was a
  list before iterating — unlike the sibling
  `build_deprecation_map_from_registry`, which already raises
  `DeprecationRegistryError` for all three cases. Root-shape validation now
  mirrors that sibling exactly, so `refresh` preserves the previously
  published deprecation map instead of silently merging nothing (or
  crashing on an unhandled `TypeError`) from a corrupt removals registry.

## [0.5.2] - 2026-08-09

### Added
- **Machine-global `config.toml`.** `~/.config/pipecat-context-hub/config.toml`
  (Windows: `%USERPROFILE%\.config\pipecat-context-hub\config.toml`), if
  present, fills any `PIPECAT_HUB_*` var not already set by a real env var
  or cwd `.env` — real env vars and cwd `.env` still win, matching the
  existing precedence. See `config.toml.example` and `docs/README.md`'s
  Environment Variables section for the full var registry and precedence
  rules. `refresh --reset-index` never deletes a config source: it aborts
  rather than `rmtree` a data directory containing the active `config.toml`
  *or* the project's cwd `.env` (so `PIPECAT_HUB_DATA_DIR=.` in a `.env`
  refuses instead of wiping the working tree).

### Changed
- **BREAKING (default behavior): `refresh` no longer deletes an
  unconfigured-this-run repo's indexed records by default.** Previously,
  any repo present in the index but absent from the current invocation's
  effective config (`PIPECAT_HUB_EXTRA_REPOS` + framework repo) was deleted
  automatically — including a repo still configured elsewhere (e.g. via
  `config.toml` or a different project's `.env`) but merely not visible
  from *this* run's directory. That's now opt-in: pass `--prune` or set
  `PIPECAT_HUB_PRUNE=1` to restore the old deletion behavior. Without it,
  `refresh` logs a warning and leaves the records and metadata in place so
  a later `--prune` can still find and remove them. Explicitly tainted
  repos are still deleted unconditionally, regardless of `--prune`. If you
  run `refresh` on a schedule (cron, CI) and relied on it auto-cleaning
  repos removed from config, add `--prune` to keep that behavior.

### Security
- **`gitpython` dependency floor bumped to 3.1.58**, fixing five
  newly-disclosed advisories (GHSA-9rj7-rf2p-w77r, GHSA-4gmw-gg2m-w46p,
  GHSA-hh9p-6wh2-4mfc, GHSA-wvpp-8hx9-p66j, GHSA-jm78-9fvv-mhgr) on top of
  the existing GHSA-3f7w-8rr8-f37f floor.

## [0.5.1] - 2026-08-06

### Added
- **CLI report-hint parity with MCP.** The one-shot CLI (`cli_query.py`,
  `cli.py`) now gives the same "where to report this" nudge on stderr that
  the MCP `initialize` instructions already give a connecting agent:
  a remediation-first retrieval-quality hint when a semantic command
  (`search-docs`, `search-examples`, `search-api`, `get-code-snippet`)
  returns `low_confidence` or an empty result collection; a bug-report hint
  appended to all three `_EXIT_INDEX_UNREADY` exits in `cli_query.py` and
  `cli.py`'s `serve` startup and `refresh` paths; and a remediation-first
  warning (naming `refresh` before the bug-report URL) when a semantic
  command or `serve` startup finds the reranker model uncached
  (`reranker_disabled_reason == "not_cached"`). Lookup commands
  (`check-deprecation`, `get-doc`, `get-example`, `status`) are unaffected —
  they are direct lookups, not ranked retrieval. Stdout JSON and exit codes
  are unchanged; all new text is stderr-only. The two GitHub issue-template
  URLs now live in a single `shared/support_links.py` module that both the
  MCP instructions and the CLI import, so a template rename or repo move
  can no longer update one copy and silently leave the other stale. The MCP
  `initialize` instructions' degraded-hub clause now carries the same
  remediation-first gap-closer: for `reranker_disabled_reason ==
  "not_cached"` it tells the connecting agent to suggest
  `pipecat-context-hub refresh` before the bug-report URL, matching the
  CLI's wording — and, since a running `serve` process resolves its
  reranker state once at startup and does not re-probe the model cache
  while running, also tells the agent that the underlying server process
  must actually be restarted after `refresh` completes before re-checking
  `get_hub_status` — a client-side reconnect that reuses the same running
  process will not help — since re-checking on the same connection, or on
  such a reconnect, still reports `not_cached`. For
  non-zero boot exits, which occur before MCP
  initialization and make `get_hub_status` unavailable, the instructions now
  tell agents to follow the remediation from startup stderr first, reconnect,
  and request `get_hub_status` only after initialization succeeds. This covers
  the existing `refresh` recovery for empty indexes and
  `refresh --force --reset-index` recovery for unreadable or incompatible
  indexes; unresolved failures still route to the bug report. New
  `tests/integration/test_report_hint_e2e.py` guards both hints end to end
  against real subprocesses (a real `serve` stdio `initialize` round-trip
  and a real CLI run against a genuinely empty index) rather than mocked
  handler responses. The retrieval-quality hint's retry clause is
  per-command: `get-code-snippet` is told to retry with a different
  `--symbol`/`--intent`/`--path` rather than a `--limit` flag it doesn't
  have, while the three search commands still name `--limit`.

### Fixed
- **The CLI now warns when a *cached* reranker model fails to load at runtime, instead of
  degrading silently.** `_maybe_warn_reranker_not_cached` only ever saw the pre-dispatch
  reranker probe, so a model that looked cached and enabled but then failed to load its
  ONNX weights during the query itself (`serve`'s `load_failed` condition) went unreported
  by the one-shot CLI — a resulting low-confidence result was mis-routed to the
  retrieval-quality tracker instead of the bug tracker, or no hint fired at all. A new
  `_maybe_warn_reranker_load_failed` reads the live `CrossEncoderReranker` instance after
  dispatch and fires a bug-report hint (not a `refresh` hint, since the model is cached),
  suppressing only the `low_confidence` half of the retrieval-quality hint the same way the
  `not_cached` warning already does.
- **The MCP `not_cached` remediation no longer says "restart or reconnect" as if the two
  were interchangeable.** Some MCP hosts keep the underlying `pipecat-context-hub` process
  alive across a client-side reconnect that only re-establishes the logical session — in
  that case `get_hub_status` still reported `not_cached` after the "reconnect" the
  instructions suggested, leaving the agent stuck in the same loop the guidance exists to
  prevent. The instructions now name restarting the underlying process as the fix and
  explicitly say a same-process reconnect will not help.
- **`install` now registers an MCP server the client can actually start.** It recorded the
  bare console-script name `pipecat-context-hub` whenever one was on `PATH`, which asks the
  client to resolve it later, in a process this command cannot see — commonly one launched
  from a GUI with no shell `PATH` at all. The registration started only in the shell that
  wrote it and failed everywhere else with `ENOENT: Executable not found in $PATH`, and did
  so silently: a coding agent whose MCP server fails to start is not told, so it answers
  from training data instead. The command recorded is now always this interpreter and
  module — an absolute path resolved once at install time, pinned to the hub you invoked:
  the Pipecat CLI's bundled copy via `pipecat context-hub install`, the standalone tool via
  `pipecat-context-hub install`.

  Preferring the console script was correct when standalone was the normal install. Since
  the hub became a dependency of `pipecat-ai[cli]`, `uv tool` exposes only the host
  package's scripts, so that branch fires only for an *unrelated* hub on `PATH` — a
  leftover standalone install, or any active project venv, since `pipecat-ai[evals]` pulls
  in `pipecat-ai[cli]`.

- **`install` reports whether it configured anything.** It exited `0` both when it
  registered the server with a client CLI and when it only printed the config for you to
  paste, so a caller could not tell the two apart — and a wrapper that captures the output,
  as `pipecat init` does, would report a registration that never happened while swallowing
  the block the user needed. It now exits `3` when nothing was configured automatically,
  which is the path every file-configured editor (Cursor, VS Code, Zed) takes, along with
  any machine that has no client CLI installed. Exit `1` still means a client CLI rejected
  the registration.

- **Reinstalling repairs a stale registration instead of skipping it.** `claude mcp add`
  refuses a name it already has and leaves the existing entry alone, so registrations
  written by earlier releases would have survived every reinstall. `install` now compares
  the recorded command with the one it would write, replaces it when they differ, and
  leaves a matching one untouched. "Already registered" is no longer reported as a failure.

- **The stale-registration repair above is now rollback-safe instead of destructive.**
  Replacing a mismatched Claude Code entry used to remove it before adding the
  replacement; if the add then failed, the previous — working — registration was
  permanently lost with no way to recover it. `install` now captures the exact entry
  before removing it and restores it via `claude mcp add-json` if the replacement fails;
  Codex is unaffected, since its `mcp add` already overwrites atomically. A repair whose
  rollback also fails now exits `4` rather than the generic `1`, so a caller scripting
  around this command can tell "the previous registration is intact" from "it's gone and
  needs manual attention" without parsing stderr. Inspection of an existing registration
  also no longer aborts `install` on every unrecognized `mcp get` failure (it used to
  break first-time installs whenever a client's error wording didn't match a hardcoded
  string) and no longer crashes on a non-dict Codex transport value or a stored
  registration with an explicit `"args": null`.

- **`redact_home_in_text` no longer corrupts report-hint URLs under a degenerate
  `HOME=/`.** In root/minimal-container environments where `HOME` is exactly the path
  separator, `home + os.sep` collapsed to a bare `"//"`, which also occurs inside every
  `https://` URL — including the report-hint URLs this function's callers append. A naive
  `text.replace("//", "~/")` mangled `"https://"` into `"https:~/"`. This degenerate home is
  now treated as nothing-to-redact instead.

- **The CLI's reranker `not_cached` warning no longer co-fires with unrelated errors or a
  retrieval-quality hint that doesn't apply.** It used to fire from inside `_query_runtime`
  immediately after resolving `reranker_status`, before dispatch and therefore before input
  validation — a semantic command with bad arguments (a pydantic `ValidationError`, exit 1)
  could print the reranker warning right next to an unrelated validation error. The warning
  now fires only after a successful dispatch, alongside the retrieval-quality hint, and the
  two no longer co-fire uncoordinated: when the reranker warning already explains a
  low-confidence result, the retrieval-quality hint no longer pairs a second, mis-routed
  nudge on top of it.

- **The CLI's retrieval-quality hint no longer goes silent on a zero-hit query just because
  the reranker is uncached.** `_maybe_warn_poor_results`'s `reranker_uncached` gate used to
  suppress both the `low_confidence` and `empty_results` halves of the hint together. An
  uncached reranker can only affect *ranking*, not whether the candidate set came back
  empty, so a cold-cache operator whose query matched nothing saw only the "reranking
  disabled" warning and no signal that their query itself found no hits. The gate now only
  suppresses the `low_confidence`-driven half.

### Security
- Bumped `cryptography` 49.0.0 → 50.0.0 (CVE-2026-69247) and `gitpython` 3.1.56 → 3.1.57
  (GHSA-3f7w-8rr8-f37f). Floors raised above the fix versions so a plain re-lock can't
  regress.

## [0.5.0] - 2026-08-03

### Changed
- **Embedding and reranking now run on ONNX Runtime instead of sentence-transformers**,
  removing `torch` from the dependency tree. The install shrinks from **1.0 GB to 310 MB
  on macOS** and from **5.1 GB to 351 MB on Linux** — a 93% reduction on Linux, where
  `torch` pulled 15 `nvidia-*` CUDA packages plus `triton`: 4.5 GB of GPU kernels to run
  a 22 MB MiniLM model that was already pinned to the CPU. `onnxruntime` and `tokenizers`
  were already installed transitively via chromadb, so nothing new was added.

  The same published model weights are used, via the official `onnx/model.onnx` exports.
  An ONNX export is a format change, not a math change: measured parity against the old
  backend is cosine `1.000000` with a max elementwise difference of 2.5e-07, and
  cross-encoder logits match to 7.6e-06 with identical ranking. **Existing indexes remain
  valid — no reindex is required.** `tests/integration/test_onnx_parity.py` pins this
  against reference vectors captured from the previous backend.

  Model topology (pooling mode, sequence length, normalisation) is read from the model
  repo rather than hard-coded, so `EmbeddingConfig.model_name` remains free-form.

  Measured A/B against the pre-migration build on the same index (macOS arm64, 18 cores):

  | | before | after |
  |---|---|---|
  | `embed_query` | 2.91 ms | **0.76 ms** |
  | `embed_texts` (64) | 18.90 ms | **14.55 ms** |
  | rerank (20 pairs) | 8.12 ms | **7.36 ms** |
  | first model load | 1765 ms | **127 ms** |
  | `serve` boot-to-ready | 2183 ms | **1043 ms** |
  | `search-docs` end-to-end | 3775 ms | **1238 ms** |
  | peak RSS (search + rerank) | 1104 MB | **908 MB** |

  Commands that do not load a model (`status`, `check-deprecation`, `--version`) are
  unchanged within noise. Pre-warm on `serve` boot is retained but is now an
  optimisation rather than a fix for 30-130s Windows cold starts.

### Fixed
- **Multi-concept CLI queries no longer crash.** On the previous backend,
  `search-docs "A + B"` (and the `search-api` / `search-examples` / `get-code-snippet`
  equivalents) failed 12 out of 12 runs with the reranker enabled — 10 SIGSEGV/SIGBUS and
  2 hangs — on macOS arm64 / Python 3.14 / torch 2.13. Multi-concept queries fan out
  concurrent per-concept searches, and the one-shot CLI has no pre-warm, so the torch
  cross-encoder was being loaded from several threads at once. Single-concept queries and
  the long-lived `serve` path (which pre-warms at boot) were unaffected, which is why this
  went unnoticed. Reproduced on both a source build and the released 0.2.1 tool install;
  the ONNX backend passes 12/12. Multi-concept search is a documented headline feature, so
  this was a total failure of it from the CLI front door.

  **Upgrade note:** the first `refresh` after upgrading downloads the ONNX weights
  (~180 MB for both models); the previously cached `.safetensors` files are not reused and
  can be reclaimed with `huggingface-cli delete-cache`. `refresh` pre-downloads both models
  so this surfaces there rather than on the first query.

- **`requires-python` upper bound removed** — now `>=3.11`, matching `pipecat-ai`. The
  hub is designed to install alongside `pipecat-ai[cli]`, and a cap is viral there: once
  `onnxruntime` ships wheels for a new Python, a capped hub would be the only reason
  `pipecat-ai[cli]` fails to resolve on it, until a hub release went out. Wheel
  availability remains the real constraint and reports itself clearly at install time.
  No behaviour change on 3.11–3.14.

### Removed
- **`sentence-transformers` and `transformers` runtime dependencies.** The `transformers`
  pin existed only as a CVE floor for a sentence-transformers transitive; nothing in
  `src/` ever imported it.

### Security
- **Dropped two permanently-unfixable `torch` advisories** — `PYSEC-2026-139` /
  CVE-2026-4538 (pt2-loader deserialization) and `CVE-2025-3000` / GHSA-rrmf-rvhw-rf47
  (`torch.jit.script` memory corruption). Both had no upstream fix and were carried as
  standing `--ignore-vuln` entries in the PR gate. Removing `torch` removes the exposure
  rather than suppressing it; the `pip-audit` ignore set is now a single chromadb entry.
- **Fixed a model-cache probe that would have misreported after this change.** The
  reranker's `is_model_cached` checked for `config.json`, which a pre-migration cache
  already contains. It now probes `onnx/model.onnx`, so an upgraded install cannot report
  a cached reranker it can no longer load offline.
- **Model downloads are now pinned to immutable commit SHAs** for every model the hub
  ships with. Previously — under both backends — downloads resolved whatever `main`
  pointed at, so a compromised or merely re-uploaded repo would be trusted silently.
  Pinning also protects index compatibility: an upstream re-export of `onnx/model.onnx`
  would otherwise start writing subtly different vectors into an index built from the
  old ones, degrading retrieval with no visible error. Custom models set via
  `EmbeddingConfig.model_name` have no pin available and resolve the default branch, as
  before.

## [0.4.0] - 2026-08-02

### Changed
- **The CLI bridge is now `pipecat context-hub`, not `pipecat mcp`** — with `pipecat ch`
  as a shorter alias, hidden from `pipecat --help` so the command is listed once. `mcp`
  named the minority feature: ten of the twelve subcommands have nothing to do with MCP,
  and it collided with pipecat's own `pipecat-ai[mcp]` extra for `MCPClient`, a
  bot-side service — a same-word, different-concept pair that the docs had to
  disambiguate on two pages. Every other name for this tool is already
  `pipecat-context-hub`: the package, the console script, the MCP server, the docs. This
  is a breaking rename of a command introduced in 0.3.0 two days earlier, made now
  precisely because nothing depends on it yet.

## [0.3.1] - 2026-07-26

### Fixed
- **`pipecat mcp --help` no longer truncates command descriptions** — the bridge asked
  click for each command's summary without a length, taking the 45-character default
  that click itself applies only after sizing it to the terminal. Descriptions that the
  direct CLI renders in full were ellipsised through the Pipecat CLI. The bridge now
  asks for the whole line and lets the help renderer wrap it.

## [0.3.0] - 2026-07-26

### Added
- **Mounts into the Pipecat CLI as `pipecat mcp`** — installing this package alongside
  `pipecat-ai[cli]` exposes every command as `pipecat mcp <command>`, with identical
  parsing, JSON, and exit codes. Entry-point discovery is dynamic, so this works against
  a Pipecat CLI that is already installed; no upgrade is needed on that side.
  `uv tool install "pipecat-ai[cli]" --with pipecat-ai-context-hub`. The bridge
  (`plugin.py`) registers one passthrough per click command and hands raw argv to the
  click group rather than restating commands in Typer, which would drift every release —
  and, because typer vendors a private copy of click whose exception types differ from
  the real ones, keeping all parsing inside click is what makes usage errors, subcommand
  `--help`, and exit codes behave the same through either front door. `typer` is a peer
  dependency provided by `pipecat-ai[cli]`, not a runtime dependency of this package.
- **`install` command** — registers the MCP server with a coding agent and builds the
  index in one step, replacing four manual ones. Claude Code and Codex are configured
  through their own CLI so they own the edit to their config file; Cursor, VS Code, and
  Zed get the exact JSON and its location printed, since this command will not write to
  a config file it does not own. `--print-config` shows what would be registered and
  changes nothing; `--no-refresh` skips the index build. The registered command starts
  this package directly — the `pipecat-context-hub` console script when it is on PATH,
  otherwise the running interpreter's `-m pipecat_context_hub` — so serving never loads
  the Pipecat CLI, and a co-install (where `--with` exposes no script for this package)
  stays pinned to the installed version rather than resolving the latest one via `uvx`
  at every start.
- **`start` as an alias for `serve`** — reads naturally as `pipecat mcp start`.
- **`index_metadata` is a published read contract** — external tooling can answer "how
  old is this index, and which pipecat version is it for" by reading
  `<data dir>/metadata.db` read-only with the standard library, without importing the
  hub. Every in-process query path constructs an `IndexStore`, which opens ChromaDB even
  for lookups that need no embeddings, so neither importing the package nor shelling out
  to `status` is cheap enough to run on every invocation of another tool. The database is
  WAL, so a reader neither blocks nor is blocked by a concurrent `refresh` or a running
  `serve`. Refresh now stamps `metadata_contract_version`; the contracted keys, the
  required `PIPECAT_HUB_DATA_DIR` handling, and the compatibility rules are documented
  under "Index Metadata Contract" in `docs/README.md`.
- **The index records which pipecat revision it was built from** — every refresh now
  stamps `indexed_framework_version` (the nearest release tag, e.g. `1.6.0`) and
  `indexed_framework_commits_ahead` (the distance from that tag to the indexed commit),
  both surfaced by `get_hub_status`. Previously the only version signal was
  `framework_version`, which records an operator's explicit `--framework-version` pin and
  is *deleted* on any unpinned refresh — so a default index said nothing about which
  release it reflected, and a consumer could not tell whether the index matched the
  pipecat version a project builds against. The commit distance matters because an
  unpinned refresh tracks the default branch, where the nearest tag is a floor rather than
  an identity: an index built 55 commits past `v1.6.0` still describes as `1.6.0`. The two
  keys are left untouched when the framework repo is not cloned in a run, so a transient
  clone failure keeps the last known-good stamp instead of erasing it.
- **`check_deprecation` is version-aware and reports removed symbols** — an optional
  `version` argument evaluates a symbol's lifecycle at that pipecat version, and responses
  now include a `status` (`current` / `deprecated` / `removed`) plus `announced_removed_in`.
  Once pipecat ships its `removals.json`, a symbol deleted in a release reports "removed in
  `V`, use `Y`" instead of "not deprecated"; `version` defaults to the indexed framework
  version when omitted. Dormant (no behavior change) until that file exists. The one-shot
  CLI exposes this via `check-deprecation <symbol> --at-version <V>`.
- **`pipecat-context-hub --version`** — prints the running package version and
  exits, with no index open or model load. Previously the only way to read the
  version was `status` (`server_version`), which exits 2 when the index is
  missing/empty — so on a fresh `uvx pipecat-ai-context-hub@latest` machine,
  before `refresh` has built an index, the version was unreadable. Sourced from
  `importlib.metadata`, so it always matches `_SERVER_VERSION`.

### Changed
- **`location` documented as a bare file path, not `file:line`** — aligns the
  `check_deprecation` `location` field docs with pipecat-ai/pipecat#4749, which
  stores the deprecation marker's `location` as a file path (no `:line`) so the
  registry stops churning on unrelated line shifts. No behavior change: the
  consumer surfaces `location` verbatim and never parses it, so registries that
  still carry a `:line` suffix (older pipecat) keep working unchanged.

### Fixed
- **`pipecat_context_hub.__version__` reports the real version** — it was hard-coded to
  `0.1.0` and had drifted three releases behind the package. It now derives from
  `importlib.metadata`, and a test pins it to `pyproject.toml` alongside the existing
  `_SERVER_VERSION` check, so it cannot silently drift again. External consumers reach
  for `__version__` first. Falls back to `"unknown"` (not a version-shaped `"0.0.0"`)
  when running from a source tree with no installed distribution.
- **A tainted framework repo is no longer stamped as indexed** — `indexed_framework_version`
  was recorded from the framework checkout even when its ref was tainted and never
  ingested (or its records were removed), claiming the index reflected a release it did
  not actually hold. The capture now excludes tainted repos, and removing framework
  records clears the stamp with them.
- **`get_hub_status` no longer raises on malformed index metadata** — a single
  unparseable stored value (e.g. a corrupted `indexed_framework_commits_ahead`)
  crashed the whole status call; it now degrades that field to `None` and logs a
  warning, covering `last_refresh_duration_seconds` too.
- **`describe_framework_checkout` surfaces unexpected git failures** — a broken git
  install or an unreadable checkout previously looked identical to the routine
  no-reachable-tags case. It now logs those at `warning` so they're visible at
  default log verbosity, while the routine case stays silent.

### Security
- **Batch transitive dependency bumps (PR #93)** — patches five advisories in the
  resolved lockfile, combining Dependabot PRs #90/#91/#92 plus two advisories
  disclosed afterward: `cryptography` 46.0.7→49.0.0 (GHSA-537c-gmf6-5ccf),
  `python-multipart` 0.0.27→0.0.32 (CVE-2026-53538 / CVE-2026-53539 /
  CVE-2026-53540), `msgpack` 1.1.2→1.2.1 (GHSA-6v7p-g79w-8964), and
  `pydantic-settings` 2.13.0→2.14.2 (GHSA-4xgf-cpjx-pc3j); `starlette` 1.1.0→1.3.1
  rides along (no CVE). `cryptography` is a **direct** dependency, so its
  `pyproject.toml` floor is also raised (`>=46.0.7` → `>=48.0.1`) — a lock-only bump
  would leave the published metadata admitting the vulnerable 46.0.7. The other four
  are transitive with open upper bounds (nothing caps them below the fix), so they
  are patched by re-lock with no pin, per the project's established pattern (see the
  AGENTS.md Review Checklist). `pip-audit` reports no known vulnerabilities after the
  bump.

## [0.2.1] - 2026-06-14

### Added
- **`check_deprecation` returns `kind`, `relation`, and `location`** — responses
  now carry what was deprecated (`class` / `method` / `property` / `parameter` /
  `module`), how the replacement relates to it (`rename` / `merged` / `move` /
  `use_existing` / `none`), and the source `file:line` of the deprecation marker.
  An agent gets "removed; use the existing `X`" semantics plus a
  jump-to-definition pointer instead of a bare replacement string.

### Changed
- **Deprecation data is now registry-only.** `check_deprecation` reads pipecat's
  machine-readable registry (`scripts/deprecations/deprecations.json`, shipped by
  [pipecat-ai/pipecat#4726](https://github.com/pipecat-ai/pipecat/pull/4726)) as
  the single source of truth; the release-notes / CHANGELOG prose parser has been
  removed. Consequence: a pipecat checkout that **predates** the registry now
  produces an empty deprecation map (logged, no error), so `check_deprecation`
  reports `deprecated: false` for symbols it has no registry data on. Indexing a
  pipecat version that ships the registry restores full coverage. Run
  `uv run pipecat-context-hub refresh --force` after upgrading.

### Fixed
- **`check_deprecation` no longer reports current APIs as deprecated** — the old
  prose parser's heuristics produced false positives (current classes such as
  `DailyTransport` flagged as deprecated). Registry lookups are exact: bare
  names, nested members, and fully-qualified module paths all resolve, and a
  symbol is reported deprecated only if it — or a symbol nested under it — is in
  the registry. Ancestor packages (`pipecat.services`) and owner-of-member
  classes (a class whose method/parameter is deprecated) are never flagged.
- **`get_doc()` now populates the `sections` field** — it was always empty
  because the doc ingester never persisted a `sections` metadata key. Section
  titles are now derived from the page's own markdown headings (fence-aware, so
  `#` comment lines inside code blocks are not advertised; case-insensitive
  dedup). Every advertised title round-trips through the `section=` argument.
  Query-time only — no re-index required.
- **`get_doc()` / `search_docs` now return the page's real title** — the doc
  ingester captured each page's heading but never persisted it, so the `title`
  field fell back to the URL path. The page title is now stored on every chunk's
  metadata. Requires a re-index to take effect:
  `uv run pipecat-context-hub refresh --force`.

## [0.2.0] - 2026-06-12

First release published to PyPI (`pipecat-ai-context-hub`, under the `pipecat`
org), and the first `uvx`-able version — `uvx pipecat-ai-context-hub <cmd>`.

### Fixed
- **`check_deprecation` misread "renamed to" release bullets** — the
  release-notes parser split deprecated names from replacements on the
  literal word "use" only, and extracted class names only when a bullet had
  no module paths. Every pipecat 1.3.0 rename bullet ("`PipelineTask` …
  ha[s] been renamed to `PipelineWorker`", "Import `WorkerRunner` from
  `pipecat.workers.runner`") was misread: the *replacements*
  (`pipecat.pipeline.worker`, `pipecat.workers.runner`,
  `InterruptionTaskFrame`) were keyed as deprecated — telling agents the
  current API is deprecated — while the actually-deprecated names
  (`PipelineTask`, `PipelineTaskParams`, `PipelineRunner`) returned
  `deprecated: false`. The parser now classifies every backticked module
  path *and* CamelCase symbol against a richer boundary ("use", "renamed
  to", "replaced by/with", "moved to", "import", …), with "the old `X`" /
  "the new `X`" phrasing rescues, a prose-identifier blocklist
  (`DeprecationWarning`, …), and populated `new_path` replacements.
  Releases are also now processed newest-first with numeric version
  ordering (lexicographic sorting put `0.0.9` after `0.0.108`): the newest
  mention's note/new_path are primary, the earliest mention supplies
  `deprecated_in`/`removed_in`. Verified against the live index: all 1.3.0
  renames now resolve correctly in both directions.
  Two further lifecycle corruptions found in live follow-up: historical
  *member* bullets keyed the owning class ("`PipelineTask` events … are now
  deprecated" / "Removed … events from `PipelineTask`" supplied bogus
  `deprecated_in: 0.0.86` and `removed_in: 1.0.0` for the class itself) —
  fixed with owner-context detection ("of/from/on/to `X`", "`X`
  events/method/parameter/constructor…" are skipped on the deprecated
  side); and hand-written release bodies mix heading levels (v0.0.87 has
  `## Fixed` as h2), which left the Deprecated section open and parsed
  Fixed bullets as deprecations — section headings now accept any level
  (h1–h6, tolerant of a missing space after the hashes) and *any* heading
  closes the current section. A heading that mentions "deprecated"/"removed"
  but does not parse as a section now **warns** at refresh instead of
  silently dropping the section (pipecat's changelog has a human step that
  can introduce malformed headers). A rename written *source*-first
  ("renamed from `X`") is also keyed correctly now, not dropped by the
  owner-context skip. `PipelineTask` now reports `deprecated_in: 1.3.0`,
  `removed_in: null`, matching the pipecat changelog.
- **`check_deprecation` reported ~17 current APIs as deprecated** (owner-of-
  member false positives) — the worst failure mode the tool has. Owner-context
  detection only recognised a single token right after a preposition, so other
  member-deprecation phrasings leaked the owning class: `` `TTSService`:
  `text_aggregator` init param `` (colon header), `` `GladiaSTTService`'s
  `confidence` arg `` (possessive), `` `SimliVideoService` `simli_config`
  parameter `` (adjacent owner+member), `` `english_normalization` parameter
  for `MiniMaxHttpTTSService` `` (trailing `for X`), `For `SpeechmaticsSTTService`,
  the `…` parameter` (subject clause), and delimiter-joined owner lists
  (`from `PipelineParams`, `StartFrame`, and `FrameProcessor``, `from `A` /
  `B``) where only the first token was skipped. All are now treated as owner
  context. The colon-header convention skips *every* class token in the bullet
  (so nested `` `InputParams` class `` members no longer leak), and "newer"/
  "latest" join the replacement markers. Separately, `check()`'s reverse-prefix
  fuzzy match no longer fires for a bare class name, so an owner class
  (`GladiaSTTService`) is not reported deprecated just because a nested member
  (`GladiaSTTService.InputParams`) is. Genuine removals are preserved (verified
  by a full release-notes rebuild: 17 false-positive keys dropped, zero genuine
  removals lost). This entry fixes the **owner-of-member** class of current-API
  false positive; a separate "replacement-kept" class remains (see Known
  limitations below).

### Known limitations
- **`check_deprecation` still mis-keys 5 "replacement-kept" current APIs** — a
  class named *only as a still-usable alternative* inside a removal bullet is
  reported `deprecated: true` because the parser defaults every API token
  before the boundary to deprecated and has no concept of a kept replacement on
  the deprecated side: `OpenAILLMService` ("can still be used with
  `OpenAILLMService`"), `WebsocketTTSService` ("Subclass `WebsocketTTSService`
  directly"), `TTSService` ("part of the base `TTSService`", 0.0.105 bullet),
  `LLMContext` ("now built into `LLMContext`"), and `LocalSmartTurnAnalyzerV3`
  ("`transformers` now always installed"). The fix is deferred deliberately: a
  phrase-matching rescue is two-sided risk (words like "still"/"directly"/"into"
  also appear in genuine removal bullets, so a too-broad rule would suppress
  *real* deprecations — a silent false negative), and warrants semantic phrasing
  detection scoped to its own change with a measured rebuild diff. Tracked in
  AGENTS.md item #48 and enumerated in `scripts/smoke_check_deprecation.py`
  (`--known-gaps`).

### Added
- **Staleness footer on tool responses** — when the local index is older than
  a threshold (default 7 days; `PIPECAT_HUB_STALE_AFTER_DAYS`, `0` disables),
  every tool response on both front doors (MCP and the CLI subcommands)
  carries an `index_staleness` field with `last_refresh_at`, `age_days`, and
  a refresh hint. Absent when fresh, so the common case carries zero noise;
  `get_hub_status`/`status` is never annotated (it *is* the staleness
  report). Closes the invisible-failure mode where queries keep succeeding
  against a quietly outdated index, without relying on callers to poll
  status. Best-effort by construction: annotation can never break a
  response.
- **CLI query subcommands** — every MCP tool is now also a one-shot shell
  command: `search-docs`, `get-doc`, `search-examples`, `get-example`,
  `get-code-snippet`, `search-api`, `check-deprecation`, and `status`
  (`get_hub_status`). Same index, same retrieval stack, same tool handlers as
  `serve`; stdout carries exactly the handler's JSON, logs/errors go to
  stderr. Exit codes: 0 success, 1 invalid input, 2 index missing/empty
  (with an actionable `refresh` hint). The embedding model and reranker load
  only for the semantic commands; the lookup commands (`check-deprecation`,
  `get-doc`, `get-example`, `status`) run in under a second. This gives
  coding agents a zero-configuration path to the hub — `pipecat-context-hub
  check-deprecation PipelineTask` from any shell — with the MCP server
  remaining the warm, session-long front door. A parity test enforces that
  every MCP tool has a CLI command, so the two surfaces cannot drift.
  Query commands are quiet by default: logging downgrades to WARNING (an
  explicit `--log-level` wins), third-party model-loading chatter (progress
  bars, transformers load reports) is silenced, and `HF_HUB_OFFLINE=1` skips
  huggingface_hub's network revalidation of already-cached models — cutting
  semantic-query latency roughly in half (~5.4s → ~2.8s) and keeping a
  one-shot command's captured output to exactly the JSON payload.
  The offline/quiet model loading also applies to `serve` (shared helper
  `shared/model_loading.py`): faster boot (embedding pre-warm ~1.4s, no
  network), and MCP logs keep the one-line telemetry without progress bars
  or load reports. `refresh` is deliberately excluded — it is the code path
  that downloads models, and its progress bars are for the human watching a
  multi-minute run. All env defaults use `setdefault`, so an explicit
  environment (e.g. `HF_HUB_OFFLINE=0`) always wins.
  User-facing error output is home-path redacted (`shared/paths.py`'s
  `redact_home` / `redact_home_in_text`) at every `cli_query` stderr site and
  the `serve`/`refresh` error branches, so a pasted bug report never leaks the
  local username or filesystem layout — including paths embedded inside an
  exception message, not just a directly-interpolated `data_dir`. Every one of
  the 8 subcommands has a per-command dispatch test (lookup commands assert no
  embedding model is constructed), so a mis-wired handler cannot pass CI.
- **PyPI release workflow** — publishing a GitHub release now builds, verifies,
  and uploads the package to PyPI via trusted publishing (OIDC, no stored
  tokens). The build job checks distribution metadata (`twine check --strict`),
  enforces that the release tag matches the version baked into the wheel, and
  smoke-tests the built wheel from a clean venv outside the checkout (console
  script runs; `serve` on an empty index exits 2 with the refresh hint). A
  manual `workflow_dispatch` publishes to TestPyPI as a dry run. See
  `docs/CONTRIBUTING.md` "Release Process" for the flow and the one-time
  trusted-publisher setup.
- **Distribution renamed to `pipecat-ai-context-hub`** ahead of the first PyPI
  release, matching the org convention (official packages are `pipecat-ai*`).
  Nothing user-facing changes after install: the command, MCP server name,
  data dir, and env vars all stay `pipecat-context-hub`, and a new
  console-script alias makes `uvx pipecat-ai-context-hub <cmd>` resolve
  directly. Clone-based installs are unaffected (`uv sync` picks up the new
  name; both command spellings work).
- **RN transports added to default ingest set** — `pipecat-ai/pipecat-client-react-native-transports`
  (TypeScript, tree-sitter-indexed), verified to yield parseable chunks before
  being added. Fills the React Native client-transport gap.

### Changed
- **Agent-oriented CLI `--help` text** — the one-shot query subcommands now
  document what an agent needs to drive them cold: the root command lists the
  stdout/stderr split and exit codes (0 success / 1 invalid input / 2 index
  missing — run `refresh`); `refresh` notes the first-run model download and
  that GitHub release-note ingestion (deprecation data) needs an authenticated
  `gh`; `search-examples` filter help enumerates the real enum values
  (`--domain` backend/frontend/config/infra, `--language` python/typescript,
  `--execution-mode` local/cloud) and marks `--foundational-class` as a legacy
  filter that silently excludes new-layout examples; `search-docs` /
  `search-examples` point at the `doc_id` / `example_id` to chain into `get-doc`
  / `get-example`; and `get-code-snippet` states its three mutually-exclusive
  lookup modes. Help text only — no behavior change.
- **Shared reranker startup resolution** — the config + HF-cache decision that
  enables/disables the cross-encoder reranker at boot now lives in one place
  (`shared/reranker.py::probe_reranker`), used by both `serve` and the one-shot
  CLI so the two front doors cannot drift on which reasons disable the reranker.
  `RerankerConfig.requested_model` is the canonical accessor for the operator's
  raw requested model (surfaced by `get_hub_status` as `configured_model`),
  replacing duplicated env-or-field derivation in each caller.
- **`pipecat-ai/pipecat-cli` kept opt-in, not default** — evaluated for the
  default set but left as a `PIPECAT_HUB_EXTRA_REPOS` extra. The CLI is consumed
  via commands (`pipecat init`, `pipecat cloud deploy`), not imported as a
  library, and its command reference is already indexed from `docs.pipecat.ai`
  (`/api-reference/cli/*`). Ingesting the repo source only added CLI-internal
  plumbing to `search_examples` / `search_api` without improving CLI-usage
  answers, so it is documented as opt-in instead.
- **Documented ingest language limits** — `README`, `CONTRIBUTING`, `.env.example`,
  and the `SourceConfig` docstring now state that only Python (`.py`/`.pyi`),
  TypeScript (`.ts`/`.tsx`), and RST are parsed. Swift/Kotlin/C++ client SDKs
  (`pipecat-client-ios`, `pipecat-client-android`, `pipecat-client-cxx`,
  `pipecat-esp32`) clone but yield zero source/API chunks (only a few
  README/config fallback chunks), so they never reach `search_api` until a
  grammar for those languages exists. Curated opt-in extras
  (`pipecat-mcp-server`, `pipecat-flows-editor`, `pipecat-krisp`) are documented
  in the `SourceConfig` docstring.

### Security
- **Dependency bumps** — `pip` `26.1.1 → 26.1.2` (PYSEC-2026-196) and `pyjwt`
  `2.12.1 → 2.13.0` (PYSEC-2026-175/177/178/179, via `mcp`'s `pyjwt[crypto]`),
  resolving the `pip-audit` findings surfaced on this branch. Both were open
  upper bounds, so a targeted re-lock pulled the fixes with no constraint pin.
- **`chromadb` CVE-2026-45829 audited, not exploitable** — pre-auth RCE in
  chromadb's optional HTTP-server mode; the hub uses only the embedded
  `PersistentClient` and never starts an HTTP endpoint. Added
  `--ignore-vuln CVE-2026-45829` to the `pip-audit` gate (ci.yml) and the
  `audit-deps` justfile recipe; documented in the AGENTS.md Review Checklist.
- **pip-audit ignore parity enforced** — `tests/unit/test_audit_sync.py` fails
  the suite if the `justfile` and `ci.yml` `--ignore-vuln` sets drift, closing
  the gap that had left a stale `CVE-2026-1839` ignore in the justfile after CI
  dropped it (now resolved: `transformers 5.5.0`).
- **`torch` CVE-2025-3000 audited, not exploitable** — memory corruption in
  `torch.jit.script` (GHSA-rrmf-rvhw-rf47 / PYSEC-2025-194). Low severity, no
  upstream fix released (vulnerable `<= 2.12.0`, no patched version). Unreachable
  here: the hub uses sentence-transformers embeddings and the cross-encoder
  reranker and never calls `torch.jit.script`. Added `--ignore-vuln CVE-2025-3000`
  to the `pip-audit` gate (ci.yml) and the `audit-deps` justfile recipe; the
  unfiltered biweekly `security-audit.yml` job re-surfaces it if a fix ships.

## [0.1.1] - 2026-05-30

### Changed
- **Python 3.14 support** — lifted the `requires-python` ceiling from `<3.14` to
  `<3.15`. The `<3.14` cap (PR #65) was a workaround for chromadb 0.6.x's
  Pydantic-v1 shim crashing on 3.14 and the absence of cp314 wheels for the
  compiled stack. Both are resolved: chromadb 1.x (pydantic-v2) shipped in
  v0.1.0, and torch 2.12 / tokenizers 0.23 / onnxruntime 1.26 now publish cp314
  wheels for macOS, Linux, and Windows. Full suite passes on 3.14.5 (1071
  passed). CI now runs the quality and Windows-smoke jobs on a 3.12 + 3.14 matrix.

## [0.1.0] - 2026-05-30

> **ChromaDB 1.x upgrade — on-disk format break.** Migrates the vector store from
> chromadb 0.6 to 1.5.x. The 1.x on-disk format is **not** backward-compatible:
> existing indexes will be refused at startup with a clear error. After
> upgrading you **must** rebuild the index:
>
> ```
> uv run pipecat-context-hub refresh --force --reset-index
> ```
>
> This release also batches the `serve` watchdog fixes and default-source
> updates that were held back for the migration (PRs #67, #69).

### Added

- **Pre-1.0 index detection.** A non-mutating probe inspects the persisted
  SQLite schema before opening it and raises a typed
  `IncompatibleIndexFormatError` naming the format mismatch and the
  `refresh --force --reset-index` remediation, surfaced by both `serve` and
  `refresh`. The 0.6 directory is left byte-identical (no silent overwrite).
- **`PIPECAT_HUB_DATA_DIR`** environment variable to override the local data
  directory (defaults to `~/.pipecat-context-hub`), for isolated/throwaway
  corpora.

### Changed

- **ChromaDB pinned `>=1.5,<2.0`** (was `>=0.6,<1.0`), resolving to 1.5.9. The
  vector-store backend is otherwise behaviourally unchanged (cosine similarity,
  the `latest` collection, query/upsert semantics). `VectorIndex` teardown now
  uses chromadb's public `Client.close()` instead of the private 0.6 internals.
- **Leaner dependency tree.** chromadb 1.x drops its embedded server stack, so
  `posthog`, `fastapi`, `asgiref`, `chroma-hnswlib`, and several
  `opentelemetry-instrumentation-*` packages are no longer installed. Removing
  `posthog` also removes one telemetry/CVE surface.
- **Idle watchdog is now a fallback, not the default orphan-cleanup path**
  (PR #69). When reliable client-death detection is active (direct-parent
  launch, or an intermediate launcher with a resolved grandparent), `serve`
  disables the idle timeout — it would only kill a warm hub mid-session. It
  stays armed when detection is unavailable (Windows, parent-watch disabled, or
  an unresolved grandparent). Set `PIPECAT_HUB_IDLE_TIMEOUT_SECS` to force an
  idle backstop back on; an explicitly-configured value is always honored.
- **Updated default indexed sources** (PR #67). Added `pipecat-ai/pipecat-flows`
  (conversation flow framework). Renamed `pipecat-ai/small-webrtc-prebuilt` to
  its new slug `pipecat-ai/pipecat-prebuilt`. Removed `pipecat-ai/pipecat-flows-editor`
  and the archived `pipecat-ai/web-client-ui` from the defaults. Run
  `refresh --force` to re-index; data for removed repos is cleaned up automatically
  on the next refresh. Any of these can still be re-added via `PIPECAT_HUB_EXTRA_REPOS`.

### Fixed

- **`serve` no longer self-terminates mid-session under `uv run`** (PR #69). When
  launched as `uv run pipecat-context-hub serve`, `uv` lingers as an
  intermediate parent, so `os.getppid()` never flips when the real client
  dies — the parent-death watchdog could not fire, and the 30-minute idle
  timeout was the only (blunt) backstop. It would reap a perfectly healthy
  hub during a quiet stretch of an active session, after which the client
  had to cold-start a new one. `serve` now detects an intermediate launcher
  (`uv`/`uvx`/`pipx`/`poetry`/`pdm`/`hatch`/`rye`/`pipenv`) and watches the
  **grandparent** (the real client) for death instead, so it exits promptly
  when — and only when — the client actually goes away. Closes the "Known
  Gap: `uv run` wrapper" from the v0.0.18 orphan-watchdog work.
- **No more spurious "leaked semaphore" warning on watchdog shutdown** (PR #69). A
  watchdog-triggered exit calls `os._exit(0)`, which skips `atexit` and left
  loky/multiprocessing (reached via the cross-encoder → `torch`/`sklearn`)
  resource-tracker semaphores unreleased, printing a benign-but-alarming
  `resource_tracker: ... leaked semaphore` warning. `serve` now runs
  `atexit` handlers in a bounded daemon thread before the hard exit. The
  hard-exit stderr line was also reworded so a normal client-gone fast-exit
  no longer reads as a crash.
- **Clear error when an older chromadb has corrupted a 1.x index dir.** If a
  chromadb **0.6** process (e.g. a stale global `pipecat-context-hub` install)
  writes to a 1.x data directory, it leaves a collection config that 1.x cannot
  parse — chromadb raises an opaque `KeyError: '_type'` deep in its sysdb on the
  next open. The pre-open migration probe can't see this (the `migrations` table
  is still 1.x; only the `collections` row is damaged), so `VectorIndex` now
  catches the config-parse failure and raises the typed
  `IncompatibleIndexFormatError` with the `refresh --force --reset-index`
  remediation, the same as the pre-1.0-directory case.
- **Logical paths are now forward-slash on every platform.** The GitHub ingest
  built repo-relative `path` / `source_url` / taxonomy-lookup keys with
  `str(Path.relative_to(...))`, which yields backslashes on Windows — producing
  malformed `source_url`s and breaking taxonomy joins there. These now use
  `Path.as_posix()` (matching `source_ingest`), so stored paths are
  `"/"`-separated regardless of OS. The Windows CI job now runs the previously
  path-sensitive test files (`test_taxonomy`, `test_github_ingest`, `test_cli`,
  `test_hub_status`) to keep this from regressing.

### Security

- **chromadb CVE-2026-45829 (GHSA-f4j7-r4q5-qw2c) — audited, not exploitable
  here, no fix available.** chromadb 1.0.0–1.5.9 carry a pre-authentication code
  injection in **HTTP server mode**: an unauthenticated attacker can execute code
  via the `/api/v2/.../collections` endpoint by supplying a malicious model repo
  with `trust_remote_code=true`. There is no fixed release yet (1.5.9 is the
  latest 1.x). The hub runs the **embedded `PersistentClient` only** — no server,
  no HTTP endpoint, no network listener — and never uses chromadb's embedding
  functions or `trust_remote_code`, so the vulnerable path is unreachable. The CI
  dependency audit ignores it with this justification; the unfiltered biweekly
  `security-audit.yml` re-surfaces it so the ignore cannot outlive an upstream
  fix.

### Notes

- **Python ceiling unchanged** (`requires-python = ">=3.11,<3.14"`). Lifting it
  to 3.14 is a separate follow-up release gated on torch + onnxruntime cp314
  wheels.

## [0.0.20] - 2026-05-29

> **Security + compat release.** Batches four upstream security bumps that
> accumulated since v0.0.19 and caps `requires-python` so the resolver doesn't
> attempt Python 3.14 with the current compiled stack. No functional changes;
> no re-index required.

### Changed

- **Capped `requires-python` to `>=3.11,<3.14`** (PR #65). chromadb 0.6.x's
  Pydantic-v1 `BaseSettings` shim crashes under Python 3.14, and torch (reached
  via `sentence-transformers` / `transformers`) has no cp314 wheels yet, so
  installs on 3.14 would either fail to resolve or fail at import. The cap will
  lift once the chromadb 1.x migration lands and the compiled stack ships 3.14
  wheels. Users already on 3.11–3.13 are unaffected.

### Security

- **Bumped `urllib3` to `2.7.0`** in `uv.lock` (transitive via `chromadb` → `kubernetes` / `posthog` → `requests`; no top-level constraint required) to address two high-severity advisories: decompression-bomb safeguard bypass in the streaming API (GHSA-mf9v-mfxr-j63j, Dependabot alert #15) and sensitive-header forwarding across origins via `ProxyManager` redirects (GHSA-qccp-gfcp-xxvc, Dependabot alert #16). Closed via Dependabot PR #62. No exploitable path from this codebase: the hub uses `httpx` for outbound HTTP and never calls `urllib3` / `ProxyManager` directly.
- **Bumped `idna` to `3.15`** in `uv.lock` (transitive via `anyio` / `httpx` / `requests`; no top-level constraint required) to address a medium-severity advisory: specially crafted inputs to `idna.encode()` can bypass the CVE-2024-3651 mitigation and trigger quadratic-time processing (CVE-2026-45409, GHSA-65pc-fj4g-8rjx, Dependabot alert #17). Closed via Dependabot PR #64. No exploitable path from this codebase: the hub never calls `idna` directly, and the hostnames reached via `httpx` / `requests` are fixed, trusted endpoints (GitHub, docs sources), not attacker-controlled input.
- **Bumped `starlette` to `1.1.0`** (transitive via `mcp` / `fastapi` / `sse-starlette`) to address a medium-severity advisory: missing Host-header validation poisons `request.url.path`, bypassing path-based security checks (PYSEC-2026-161, GHSA-86qp-5c8j-p5mr). A plain re-lock would regress to the vulnerable `0.52.1` (the prior `fastapi` capped it), so the fix is held by a `[tool.uv] constraint-dependencies` floor of `starlette>=1.0.1` rather than a lock-only bump; this also pulled `fastapi` `0.129.0` → `0.136.3`. No exploitable path from this codebase: the hub speaks MCP over stdio and never serves HTTP via Starlette.
- **Ignored `torch` advisory PYSEC-2026-139 (CVE-2026-4538)** in the PR-gating `pip-audit` step — a local-only deserialization in the pt2 loading handler with no upstream fix released. `torch` is reached only via `sentence-transformers` for local embeddings, and the hub never loads untrusted `pt2` artifacts. The unfiltered biweekly `security-audit.yml` job continues to surface it so the ignore is removed as soon as a patched release lands.

## [0.0.19] - 2026-05-08

> **Security-only release.** Resolves three open Dependabot advisories. No
> functional changes; no re-index required.

### Security

- **Bumped `pip` to `26.1.1`** to resolve CVE-2026-3219 (PR #57).
- **Bumped `gitpython` to `>=3.1.49`** (lock resolves to `3.1.50`) to address a path-traversal advisory in reference APIs allowing arbitrary file write/delete outside the repository (high severity, Dependabot alert #12, PR #59).
- **Bumped `python-multipart` to `0.0.27`** in `uv.lock` (transitive via `mcp`; no top-level constraint required) to address a DoS advisory via unbounded multipart part headers (high severity, Dependabot alert #13, PR #61).

## [0.0.18] - 2026-04-26

> **Upgrade:** run `uv run pipecat-context-hub refresh --force` after upgrading
> to clear stale `foundational_class` values pointing at paths that no longer
> exist upstream.

### Added

- **Idle-timeout shutdown for `serve`** — the server now exits on its
  own when no MCP request arrives for `PIPECAT_HUB_IDLE_TIMEOUT_SECS`
  seconds (default `1800`, i.e. 30 minutes). Catches the production
  failure mode the parent-death watchdog cannot: when the client stays
  alive but stops using a hub it spawned without closing the pipe (the
  case responsible for most accumulated zombies under `uv run`). Both
  `tools/list` and `tools/call` reset the idle clock, so sessions that
  only poll capabilities without dispatching a tool still count as
  active. Set `PIPECAT_HUB_IDLE_TIMEOUT_SECS=0` to disable. Logs
  `idle_timeout idle_seconds=N timeout_seconds=N` at INFO when it fires.
- **Offline smoke-test fixtures** under `tests/fixtures/smoke/` with reusable
  invariant helpers in `tests/smoke/invariants.py`. PR-gating tests exercise
  discovery + taxonomy against vendored tree-only snapshots of
  `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`; no network, no
  embedding model, no Chroma. Wall time ≤ 15 s.
- **Scheduled upstream-drift workflow** (`.github/workflows/smoke-drift.yml`)
  runs every 5 days against upstream `main`, reusing the same invariant
  helpers via `scripts/check_pipecat_drift.py`. On failure it opens (or
  updates in place) a single `upstream-drift`-labelled tracking issue via
  `gh` CLI — does not gate PRs. The `upstream-drift` label is created
  idempotently on first use so the very first failure can always file its
  notification.
- **SHA-ref support** in both `scripts/check_pipecat_drift.py` and
  `tests/smoke/refresh_fixtures.py`: `--ref` accepts branch, tag, or commit
  SHA. Named refs use `git clone --depth 1 --branch`; SHA refs fall through
  to `git init` + `git fetch --depth 1 <sha>` + `git checkout FETCH_HEAD`
  (GitHub allows `uploadpack.allowAnySHA1InWant`). Slug and ref are
  regex-validated and passed after a `--` sentinel; subprocess calls have
  a 300 s timeout.
- **Symlink safety** in `tests/smoke/refresh_fixtures.py::_copy_filtered`
  and in the taxonomy `_scan_topic_tree` walk — symlinks inside untrusted
  upstream clones are never followed, including grandchildren reached
  through a symlinked topic dir.
- **Layout-aware fixture refresh.** `_rebuild_fixture` handles both
  topic layout (`examples/<topic>/<example>/`) and root layout
  (`pipecat-examples`-style, where each top-level dir is an example),
  skipping the packaged-project set (`src`, `tests`, `docs`, `scripts`,
  `dashboard`, `.github`, `.claude`) so the vendored fixture never
  captures source trees.
- **Unit coverage for the new scaffolding** in
  `tests/unit/test_smoke_scaffold.py`: root-layout vs topic-layout
  `_rebuild_fixture`, SHA-vs-named-ref clone argv, slug/ref validation,
  `subprocess.TimeoutExpired` surfacing, and symlink rejection in both
  `_copy_filtered` and `_scan_topic_tree`.

### Changed

- **`serve` lifetime knobs are now first-class `ServerConfig` fields** —
  `idle_timeout_secs` and `parent_watch_interval_secs` join the existing
  `transport` and `log_level` fields on `ServerConfig`, with env-aware
  computed properties matching the rest of `HubConfig`. Env-var
  resolution moved out of `transport.py` into `shared/config.py` for
  consistency. `parent_watch_interval_secs` is now floored at `0.1s`
  when non-zero (prevents misconfigured tiny values from CPU-spinning
  on `os.getppid()`). Both parsers reject non-finite values
  (`nan`/`inf`) with a warning and fall back to the field default. No
  user-visible behaviour change.
- **`IdleTracker` moved from `shared/types.py` to new
  `shared/tracking.py`** — `shared/types.py` now holds only Pydantic
  data contracts; stateful runtime helpers live in `shared/tracking.py`.
  Internal refactor only; imports in `cli.py`, `server/main.py`,
  `server/transport.py` updated accordingly.

### Deprecated

- **`foundational_class` field** on `ExampleMetadata`, `TaxonomyEntry`, and
  `SearchExamplesInput` is deprecated. The field remains readable for
  persisted indexes and the `hybrid.py` filter path, but is no longer written
  for new-layout (non-foundational) examples. Existing users should run
  `uv run pipecat-context-hub refresh --force` after upgrading to clear stale
  values pointing at paths that no longer exist upstream.

### Fixed

- **Taxonomy coverage for the new pipecat examples topic-based layout** —
  `ExampleTaxonomyBuilder.build_from_directory` now dispatches on the layout
  shape it sees on disk: foundational + sibling topic dirs (preserving the
  `v0.0.96`-era behaviour), topic-only trees (current pipecat `main`), and
  root-level layouts (`pipecat-examples`). The builder no longer emits junk
  entries for `src/`, `tests/`, `docs/`, `scripts/`, `dashboard/`, `.github/`,
  or `.claude/` when falling back at a packaged-project root. Every dir
  returned by `_discover_under_examples` now has a matching taxonomy entry,
  restoring `capability_tags` / `key_files` / `execution_mode` on example
  chunks for topic-layout checkouts.
- **Windows first-query hang** — `serve` now pre-warms the embedding
  model (and cross-encoder when enabled) during startup so the first
  MCP query no longer pays the cold-start cost. On Windows CPU a cold
  first query could hang 30-130s while `sentence_transformers` imported
  and loaded weights inside `asyncio.to_thread`, exceeding Claude
  Code's tool-permission window and surfacing as a spurious disconnect.
  Pre-warm failures are non-fatal: lazy-load paths still handle
  first-query loading. Set `PIPECAT_HUB_WARMUP=0` to skip pre-warm
  (faster boot, slower first query). Thanks to Vanessa for diagnosing
  and patching on Windows.
- **Idle watchdog no longer reaps in-flight requests** — `IdleTracker`
  now counts active tool dispatches (`begin()` / `end()`) and reports
  `seconds_since_last() == 0` while any call is active. Previously the
  clock was only touched on call entry, so a cold `search_*` /
  `get_code_snippet` that waited on `EmbeddingService` or the
  cross-encoder lazy load could exceed
  `PIPECAT_HUB_IDLE_TIMEOUT_SECS` and be killed mid-response. The
  clock is also reset on `end()` so the idle window starts at "request
  finished", not "request dispatched".
- **`exit_on_watchdog_shutdown=False` is now host-safe end-to-end** —
  the in-process / library-embedding mode previously still closed
  `sys.stdin` and armed the 2.5 s hard-exit timer, either of which
  could tear down the host process. The flag now gates every
  host-affecting action: when `False`, `run_stdio` cancels its own
  tasks, invokes the shutdown callback once, and returns
  `shutdown_reason` to the caller without touching stdin or spawning
  the timer thread.
- **Orphan `serve` processes no longer accumulate** (direct-invocation
  path) — a parent-death watchdog inside the stdio transport polls
  `os.getppid()` every 2s and triggers a clean shutdown when the MCP
  client disappears without closing stdio. The PPID is snapshotted at
  CLI entry (before IndexStore / embedding / reranker construction) so
  client deaths during startup are still detected. On trigger, stdin is
  forcibly closed to unblock MCP's internal `stdin_reader` task,
  allowing the `stdio_server` context manager to unwind and the
  `IndexStore` finally-block to close handles cleanly. The watchdog
  logs `parent_died original_ppid=N current_ppid=1` at INFO when it
  fires. Honors hidden env var `PIPECAT_HUB_PARENT_WATCH_INTERVAL`
  (seconds, default `2.0`) for tests. Disabled on Windows where
  orphan-reparent semantics differ — stdin EOF still works there.

  **Known gap:** when `serve` is launched via `uv run
  pipecat-context-hub serve` (the default in this project's docs and
  in most MCP-client configs), `uv` stays alive as an intermediate
  parent and the inner Python process's PPID never flips — the
  parent-death watchdog does not fire. The new idle-timeout (above)
  covers this case as a backstop. For instant cleanup on parent
  death, configure your MCP client to launch Python directly
  (e.g. `.venv/bin/pipecat-context-hub serve`); see the README's
  "MCP client configuration" section for examples.

- **Hard-exit backstop for Linux `mcp.stdio_server` teardown hang** —
  on Linux, `mcp.stdio_server` parks its stdin reader in
  `anyio.to_thread.run_sync(readline, cancellable=False)`. Once
  parked, the worker thread is stuck in an uninterruptible `read(0)`;
  both `stdio_server.__aexit__` and CPython's interpreter shutdown
  wait on it forever. After a watchdog fires, `run_stdio` now
  releases index handles via the shutdown callback and calls
  `os._exit(0)` directly — before `__aexit__` can hang. A daemon
  timer thread (2.5 s budget) provides a secondary backstop if the
  callback itself hangs inside Chroma close. A single-shot guard
  prevents the graceful path and the timer from invoking the
  callback concurrently when the graceful-path call is what's hung.
  The `on_hard_exit` kwarg was renamed to `on_watchdog_shutdown` to
  reflect the dual-path semantics; `exit_on_watchdog_shutdown` opts
  the CLI into the `os._exit` behaviour while keeping in-process
  callers safe.

### Security

- **lxml GHSA-vfmq-68hx-4jfw / CVE-2026-41066** — bumped `lxml` to `>=6.1.0`
  to close an XXE vector in the default configuration of `iterparse()` and
  `ETCompatXMLParser()` (`resolve_entities=True` allowed local-file reads).
  `lxml` enters the lockfile transitively via `cyclonedx-bom`; the 4.x line
  pinned `lxml<6`, so the dev floor was raised to `cyclonedx-bom>=7.3,<8.0`
  (pulls `cyclonedx-python-lib` 11.x, which allows `lxml<7`) and an explicit
  `lxml>=6.1.0` dev pin was added so future transitive bumps cannot regress
  below the patched version.

## [0.0.17] - 2026-04-20

### Added

- **Configurable cross-encoder reranker model** — new
  `PIPECAT_HUB_RERANKER_MODEL` env var selects between three allowlisted
  cross-encoder models without editing Python config:
  `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80 MB, default, balanced),
  `cross-encoder/ms-marco-MiniLM-L-12-v2` (~130 MB, higher quality), and
  `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~17 MB, fastest download, lower
  quality). Unknown values log a warning and fall back to the default — the
  server never fails to start on a misconfigured env var. Useful on slow or
  throttled HuggingFace Hub connections where the default model download
  would stall.
- `get_hub_status` now surfaces live reranker runtime state (not just
  configured intent): `reranker_enabled` reflects whether the reranker
  is actually active, `reranker_model` is the active model name,
  `reranker_configured_model` is what the operator requested, and
  `reranker_disabled_reason` (`config_disabled` | `not_cached` |
  `load_failed`) explains degraded runs. Lets operators diagnose cases
  where the selected model is not cached or failed to load without
  reading server logs.
- **Reranker disabled-at-startup warning** — when `serve` boots with the
  reranker off, a single consolidated `WARNING` log line now reports
  `reason=<config_disabled|not_cached> configured_model=<name>` plus a
  one-line remediation hint. For `not_cached`, the hint names the exact
  HuggingFace cache directory that was probed (resolved through
  `HF_HOME` / `HUGGINGFACE_HUB_CACHE` when set), so operators can spot
  cache-discovery mismatches without reading library internals.
  Operators can grep this from an MCP JSONL trace to diagnose degraded
  boots without calling `get_hub_status`.
- **Startup banner** — `serve` now logs one `INFO` line at boot reporting
  version, data directory, and the raw `counts_by_type` mapping
  (`doc` / `code` / `source` keys as they appear in FTS, rendered as
  `counts_by_type={doc=N,code=N,source=N}`). Confirms which binary is
  actually running after an upgrade and exposes partially-populated
  indexes without a separate tool call. The `data_dir` path is redacted
  to `~/…` because server instructions now encourage clients to share
  startup log lines with maintainers.
- **Degraded-hub reporting guideline** — server instructions now direct
  MCP clients to share the full `get_hub_status` response and startup
  log lines with the user (and point them at the bug-report template)
  when the hub is running in a degraded mode — specifically
  `reranker_disabled_reason ∈ {not_cached, load_failed}` or a non-zero
  boot exit. `config_disabled` is explicitly called out as a supported
  operator choice, not a degraded state, so intentional
  `PIPECAT_HUB_RERANKER_ENABLED=0` deployments are not escalated as
  incidents.

### Changed

- **`serve` fails fast on unusable indexes** — the server now exits with a
  non-zero status and a clear remediation hint when the index is empty
  (zero records) or cannot be opened, instead of starting up and silently
  returning no results. MCP clients previously hung waiting for meaningful
  responses; they now see stdio close at boot and can surface a real
  error. Run `pipecat-context-hub refresh` before `serve` on a fresh
  install; use `refresh --force --reset-index` to rebuild a corrupt index.

### Fixed

- **Corrupt clone recovery** — `refresh` now detects repo clones left in a
  half-initialized state (e.g., `.git/objects/pack/` present but `HEAD` /
  `config` / `refs/` missing after an interrupted clone), re-clones them,
  and force-re-ingests the repo even when its remote SHA matches the
  previously stored one (the prior `stored_sha == commit_sha` skip path
  would otherwise keep the empty/broken corpus in place). Affected users
  previously saw zero code/source chunks from the broken repo with no
  obvious signal. The refresh summary reports recovered repos.
- **Non-UTF-8 console safety** — `refresh`'s summary table no longer crashes
  with `UnicodeEncodeError` on Windows consoles whose code page cannot
  encode the non-ASCII glyphs it uses (U+2500 box-drawing rule, U+2014 em
  dash in empty SHA/count cells). Every such glyph is probed against the
  active `sys.stdout.encoding` and falls back to ASCII `-` when it cannot
  be encoded; UTF-8 terminals and OEM code pages that support the glyph
  (e.g. cp437 supports U+2500) are unchanged. Set `PYTHONIOENCODING=utf-8`
  to opt into the full Unicode output on Windows.

## [0.0.16] - 2026-04-07

### Added

- **Version-pinned framework indexing (Phase 4)** — `refresh --framework-version v0.0.96`
  (or `PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96` env var) indexes the framework
  repo at a specific git tag instead of HEAD. Users pinned to an older pipecat
  version get `search_api` results matching their installed API surface.
  Deprecation map stays forward-looking from HEAD/release-notes.
- `get_hub_status` now surfaces the pinned `framework_version` in its output
- **Release notes deprecation parsing** — `check_deprecation` now uses
  GitHub release notes as a primary source for deprecation data. Parses
  `### Deprecated` and `### Removed` sections from pipecat releases,
  extracts module paths and class names from backtick-wrapped text, and
  populates the deprecation map with version-attributed entries. This
  replaces the now-empty `DeprecatedModuleProxy` source as the primary
  data for `check_deprecation`.
- Release notes are fetched via `gh` CLI during `refresh` (graceful
  fallback when `gh` is unavailable)
- Replacement path extraction from "use X instead" patterns

## [0.0.15] - 2026-04-05

### Added

- **Version-aware retrieval (Phase 2)** — `search_examples`, `search_api`,
  and `get_code_snippet` now accept an optional `pipecat_version` parameter
  (e.g., `"0.0.95"`). When provided, results are scored for compatibility
  and annotated with `version_compatibility`:
  - `compatible` — user's version satisfies the chunk's constraint
  - `newer_required` — chunk requires a version the user hasn't upgraded to
  - `older_targeted` — chunk targets an older version the user has passed
  - `unknown` — no version constraint on the chunk
- **Version filtering** — `version_filter="compatible_only"` on
  `search_examples` and `search_api` excludes `newer_required` results
  (with over-fetch to maintain result count)
- **Combined penalty cap** — version penalty (`-0.05`) and staleness penalty
  are capped at `-0.10` combined, preventing double-penalization of old +
  incompatible results. Highly relevant incompatible examples still rank
  above irrelevant compatible ones.

## [0.0.14] - 2026-04-04

### Added

- **Version-aware indexing (Phase 1a)** — extract `pipecat-ai` version
  constraints from `pyproject.toml`, `requirements.txt`, and `package.json`
  during ingestion. Per-example-directory walk-upward supports monorepos
  (e.g., pipecat-examples). Framework repo derives version from
  `git describe --tags`. Stored as `pipecat_version_pin` on chunk metadata
  and surfaced in `ExampleHit`, `ApiHit`, `CodeSnippet` results.
- **`check_deprecation` MCP tool (Phase 1b)** — new tool to check whether
  a pipecat import path is deprecated. Parses `DeprecatedModuleProxy` usage
  (with bracket-expansion) from framework source and CHANGELOG
  `Deprecated`/`Removed` sections. Fuzzy symbol matching (prefix, exact,
  child). Built at `refresh`, persisted as JSON, loaded at `serve` startup.
- `packaging` added as an explicit dependency (was transitive only)

### Changed

- Server instructions now recommend `check_deprecation` when pipecat
  imports are seen
- Deprecation map automatically deleted when framework repo is not in
  `effective_repos` (prevents serving stale data)

### Security

- Symlink rejection and `resolve().relative_to()` containment guards on
  all new file-read paths: deprecation source scanner, version extraction
  manifest readers, and CHANGELOG reader
- Narrowed `except Exception` to `except (InvalidRequirement, TypeError)`
  in version parsers (bandit B112)

## [0.0.13] - 2026-03-31

### Changed

- **Tree-sitter TypeScript extraction (Phase 2)** — replaced the regex
  parser with tree-sitter-based AST extraction. Individual method chunks
  with full typed signatures now indexed for classes and interfaces.
  Supports `.ts` and `.tsx` files with separate grammar selection.
- **Method-level search** — `search_api("connect", class_name="PipecatClient")`
  now returns the individual method chunk with signature and body
- **Enhanced metadata** — `method_signature`, `return_type`, `imports`,
  and `calls` populated for TS function and method chunks
- **Removed regex parser** — `ts_source_parser.py` deleted, fully replaced
  by `ts_tree_sitter_parser.py`

### Added

- `tree-sitter` and `tree-sitter-typescript` runtime dependencies

## [0.0.12] - 2026-03-30

### Added

- **TypeScript source parsing (Phase 1a)** — regex-based extraction of
  exported interfaces, classes, type aliases, functions, enums, and typed
  const exports from `.ts`/`.tsx` files with JSDoc comment inclusion
- **6 core TS SDK repos added to default ingestion** —
  `pipecat-client-web`, `pipecat-client-web-transports`, `voice-ui-kit`,
  `pipecat-flows-editor`, `web-client-ui`, `small-webrtc-prebuilt`
- **`language="typescript"` metadata** on all TS source chunks for
  language-aware filtering in `search_api`
- **README fallback for zero-chunk repos (Phase 1c)** — repos with no code
  files (e.g. iOS/Android SDKs) now have their README indexed as
  `content_type="doc"` so they're discoverable via `search_docs`
- **Swift, Kotlin, C++ extension mappings** in `_EXTENSION_TO_LANGUAGE`
  for correct language metadata on code chunks

## [0.0.11] - 2026-03-29

### Added

- **Method-to-type cross-referencing** — Daily SDK `.pyi` method chunks now
  include `related_types` metadata linking methods to their RST type
  definitions (e.g. `send_dtmf` → `DialoutSendDtmfSettings`). Surfaced via
  `related_type_defs` on `get_code_snippet` and `related_types` on
  `search_api` results. 46 method-to-type mappings for CallClient and
  EventHandler.
- **MCP audit and hardening workflow** — committed CI quality/security jobs,
  a written MCP threat model, `just audit`, `just sbom`, and an opt-in
  runtime stability benchmark for repeated `refresh` / `serve` cycles and
  concurrent retrieval rounds
- **Local upstream taint controls** — `PIPECAT_HUB_TAINTED_REPOS` and
  `PIPECAT_HUB_TAINTED_REFS` let operators skip compromised repos, tags, or
  commit SHAs locally without waiting for upstream removal
- **Path-based `get_doc` lookup** — `get_doc(path="/guides/learn/transports")`
  returns the full assembled page without requiring a prior `search_docs` call.
  Multi-chunk pages are concatenated in insertion order with section extraction
  working on the assembled content.
- **`class_name` prefix matching** — `search_api` and `get_code_snippet` now
  match `class_name` as a prefix: `DailyTransport` finds `DailyTransport`,
  `DailyTransportClient`, `DailyTransportParams`. Both FTS and Vector backends
  updated consistently.
- **RST type documentation indexing** — `search_api` now indexes type
  definitions from `.rst` files (e.g. `types.rst` in `daily-co/daily-python`).
  Filter with `chunk_type="type_definition"` to find dict schemas, enums, and
  aliases alongside method signatures. Parses 72 Daily SDK type definitions
  including `DialoutSendDtmfSettings`, `ClientSettings`, `RecordingStreamingSettings`.
- **Pre-merge live MCP smoke test** — 10-item checklist in AGENTS.md for
  verifying retrieval correctness against the live local index before merging
- **Security policy** — `SECURITY.md` added with vulnerability reporting
  instructions and supported-version table
- **Curated `.env.example` repo bundles** — copy-ready
  `PIPECAT_HUB_EXTRA_REPOS` examples are now grouped by SDKs/transports, UI,
  flows, cloud/dev tools, quickstarts, and demos to make targeted local index
  expansion easier

### Changed

- **Locked developer setup** — install and update guidance now uses
  `uv sync --extra dev --group dev` instead of lockfile-bypassing editable
  install commands
- **Concurrent retrieval safety** — shared embedding, ChromaDB, and SQLite
  access is now serialized under load after the runtime stability benchmark
  reproduced a parallel `search_docs` crash
- **GitHub refresh safety** — repo slugs are validated before clone URLs are
  built, fetched refs are checked for taint before checkout, and tainted refs
  no longer advance local working trees
- **Least-privilege CI token scope** — the GitHub Actions workflow now declares
  explicit `GITHUB_TOKEN` permissions instead of relying on repository defaults

### Fixed

- Tainted upstream SHAs no longer overwrite last-known-good indexed metadata
  when refresh skips a compromised ref
- `llms-full.txt` is now streamed with a fixed size cap so an unexpectedly
  large upstream docs payload cannot grow refresh memory without bound
- Chroma product telemetry disabled via `NoOpProductTelemetryClient` —
  local dev tool should not phone home
- Integration tests now validate docs citations with parsed URL hostname checks
  instead of substring-style prefix matching
- Bumped `cryptography` to 46.0.6 to resolve upstream security advisory

## [0.0.10] - 2026-03-25

### Added

- **Chroma index recovery** — `refresh --reset-index` wipes and rebuilds the
  local index when persisted Chroma state is unhealthy. `IndexStore.close()`
  shuts down both backends cleanly. Benchmark health probe detects wedged
  vector state in ~16s instead of hanging for minutes.
- **`.pyi` stub file support** — `SourceIngester` now falls back to `.pyi`
  files at repo root when no Python packages exist in `src/`. Enables AST
  indexing of Rust+Python binding repos (e.g., `daily-co/daily-python`).
  `.pyi` files are only indexed by
  `SourceIngester` (not as code examples) to prevent duplicate chunks.
  Symlinks rejected + resolved-path containment checks at all file read sites.
- **Domain filtering for `search_examples`** — new `domain` filter param:
  `backend` (Python pipeline/bot code), `frontend` (JS/TS client code),
  `config` (YAML/TOML/JSON), `infra` (Docker/CI). Inferred from file path
  and language at ingestion time. Agents building Pipecat pipelines can use
  `search_examples(query="TTS", domain="backend")` to exclude frontend noise
- **Optional cross-encoder reranker** — `CrossEncoderReranker` service with
  lazy model loading, thread-safe inference via `asyncio.to_thread`, graceful
  offline degradation. Enabled by default; disable via
  `PIPECAT_HUB_RERANKER_ENABLED=0`
- **Result diversity** — repo/file diversity penalties and chunk-type
  preference for `search_api` (method > function > class > module)
- **Confidence guardrails** — `low_confidence: bool` on `EvidenceReport`,
  graduated count contribution, cross-tool suggestions, confidence floor
  with explicit `UnknownItem`

### Changed

- **`daily-co/daily-python` is now a default repo** — promoted from optional
  `PIPECAT_HUB_EXTRA_REPOS` to default sources. First-time refreshes now index
  Daily Python SDK (CallClient, EventHandler, 87 types) alongside Pipecat
  framework and examples.
- **Graduated staleness** — linear decay (max -0.10 at 365 days) replaces
  binary -0.05 at 90 days
- **UPPERCASE symbol detection** — TTS, STT, VAD, RTVI, LLM now receive
  symbol match boost
- **Dual-hit bonus** — +0.10 for chunks found by both vector AND keyword
- **Event loop** — `IndexStore` read methods offloaded to threads via
  `asyncio.to_thread` (no longer blocks event loop)

### Fixed

- FTS5 query sanitization strips double quotes to prevent syntax injection
- Cross-encoder model loading guarded by `threading.Lock` (no double-load)
- Model allowlist prevents loading untrusted models from HuggingFace

## [0.0.9] - 2026-03-23

### Added

- **Snippet enrichment for `get_code_snippet`** — `CodeSnippet` responses now
  populate three previously-empty fields from call-graph metadata:
  `dependency_notes` (per-method pipecat imports extracted from AST),
  `companion_snippets` (qualified method names called by this snippet), and
  `interface_expectations` (frame types yielded + base classes implemented).
  Computed at retrieval time — no index changes required
- **Per-method import extraction** — each method/function chunk now stores only
  the pipecat-internal imports that method actually references (via AST name
  resolution), not the entire module's import list. Fixes `dependency_notes`
  accuracy. Also improves `ApiHit.imports` precision for method/function chunks.
  Aliases (`import X as Y`) are preserved in import strings. Local imports
  inside function bodies correctly shadow module-level imports
- **Refresh summary table** — `refresh` command prints a per-source table
  showing status (updated/skipped/error), commit SHA, existing chunk count,
  and updated chunk count. Both columns sum to totals for at-a-glance
  verification
- `get_counts_by_repo()` on `FTSIndex` and `IndexStore` for pre-refresh
  chunk count snapshots
- **`AGENTS.md`** with Review Checklist for accepted design decisions

## [0.0.8] - 2026-03-17

### Added

- **Call-graph metadata** on method/function chunks: `yields` (frame types
  yielded) and `calls` (methods called via `self.method()`,
  `ClassName.method()`, `super().method()`) extracted from AST and stored
  as structured list fields
- **`yields` and `calls` filters** on `search_api` — agents can query
  "methods that yield TTSAudioRawFrame" or "methods that call push_frame"
  directly instead of falling back to `.venv` source reads
- **`yields` and `calls` fields** on `ApiHit` output — structured lists
  surfaced through MCP tool responses
- **Pipecat-internal import propagation** to class overview and method chunks,
  including relative imports (`from .utils import X`) — module overview retains
  full imports list
- **`## Yields` / `## Calls` sections** appended to method chunk text content
  for FTS keyword searchability
- `_walk_body_shallow()` iterative DFS walker that restricts extraction to
  executable function bodies — excludes decorators, parameter defaults, return
  annotations, nested functions, lambdas, and nested classes

### Changed

- `_extract_yields` only processes `ast.Yield` (not `ast.YieldFrom`) — generator
  delegation names are not frame types and were breaking the `yields` contract
- FTS `yields`/`calls` filters use JSON-key-anchored LIKE patterns with quoted
  values and closing `]` to prevent cross-field false positives
- Vector `yields`/`calls` filters use list membership post-filter (not substring
  matching on JSON dumps) for exact-match semantics
- `_extract_imports` preserves relative import dots (`from .utils` no longer
  stripped to `from utils`) via `node.level`
- `needs_post_filter` in `VectorIndex.search()` updated to include `yields`
  and `calls` for over-fetch when post-filtering

## [0.0.7] - 2026-03-11

### Added

- **Incremental refresh**: `refresh` now tracks docs content hash and per-repo
  commit SHAs. Unchanged sources are skipped entirely, reducing refresh time
  from ~90s to ~23s when nothing changed
- **`--force` flag** on `refresh` command to bypass all skip logic and force a
  full re-ingest
- **`delete_by_repo()`** on `VectorIndex`, `FTSIndex`, and `IndexStore` for
  targeted per-repo index cleanup (replaces blanket `delete_by_content_type`
  for changed repos)
- **Symbol lookup filter cascade** in `get_code_snippet`: tries exact
  `class_name` filter, then `method_name` filter, then semantic fallback —
  gives precise class/method matches before falling back to hybrid search
- `method_name` filter support in `VectorIndex._build_where_clause`
- `delete_metadata()` on `FTSIndex` and `IndexStore` for removing stale
  metadata keys (e.g. cached SHAs for removed repos)
- **Removed-repo cleanup**: `refresh` detects repos no longer in
  `effective_repos` and deletes their stale index data and metadata
- **`module` and `class_name` filters** on `get_code_snippet`: symbol lookups
  can be scoped by module path prefix (e.g. `module='pipecat.runner.daily'`)
  and/or class name, matching the filtering already available in `search_api`
- **`content_type` override** on `get_code_snippet`: intent and path lookups
  can set `content_type='source'` to search framework code instead of examples
- **`max_length` constraints** on all MCP tool string input fields to prevent
  oversized inputs reaching SQLite LIKE and ChromaDB queries
- **`chunk_type` Literal enum** on `SearchApiInput` — rejects invalid values
  at validation time and exposes the enum in the JSON schema
- **Per-element tag constraint** on `SearchExamplesInput.tags` — each tag
  capped at 64 characters

### Changed

- `get_code_snippet` `max_lines` default raised from 50 to 100 — covers 97%
  of method chunks without truncation (P90=56, P95=77 across 4,268 indexed
  methods).  Large methods like `configure()` (180 lines) still need an
  explicit `max_lines=200+`
- `search_docs` `area` filter now maps to a path prefix query (previously
  accepted but silently ignored by both index backends)
- `get_example` `include_readme` now returns stored `readme_content` from
  chunk metadata (previously always None due to ingest gap — content is now
  stored during GitHub ingestion, capped at 64 KB)
- Tool descriptions for `search_docs`, `get_doc`, `search_examples`,
  `search_api`, and `get_code_snippet` updated to document available filters
  and parameter usage

- `refresh` now ingests repos individually for per-repo error tracking instead
  of batch-ingesting all changed repos at once
- `clone_or_fetch` and `fetch_llms_txt` made public APIs (called by CLI for
  incremental hash/SHA comparison before deciding to ingest)
- CLI passes prefetched data (docs text, repo paths) to ingesters, eliminating
  redundant network fetches during refresh

### Fixed

- Docs content hash no longer persisted after errored ingest — prevents
  skipping broken docs on the next run
- Repo commit SHA no longer persisted after errored ingest — prevents skipping
  failed repos on the next run
- All `IndexStore` delete methods (`delete_by_content_type`, `delete_by_repo`,
  `delete_by_source`) now wrap FTS calls in error guards with divergence logging
- Cached repo SHA invalidated when `--force` ingest fails — prevents the next
  non-force refresh from skipping a repo left empty by a transient failure
- LIKE metacharacters (`%`, `_`, `\`) now escaped in all FTS filter patterns —
  prevents silent filter bypass from user input containing wildcards
- Explicit `device="cpu"` on `SentenceTransformer` init — avoids torch 2.10+
  meta tensor errors in long-running MCP server processes

### Removed

- Dead `path` field from `GetExampleInput` (was declared but never read)
- Dead `framework` and `example_ids` fields from `GetCodeSnippetInput`

## [0.0.6] - 2026-03-06

### Added

- **Multi-repo source indexing**: `SourceIngester` parameterized by repo slug —
  all repos with `src/` layouts now get AST-indexed, not just `pipecat-ai/pipecat`
- **Flat example file indexing**: repos with `.py` files directly in `examples/`
  (no subdirectories) are now discovered and indexed

### Changed

- `get_code_snippet` symbol lookups now search `content_type="source"` (framework
  API definitions) instead of `content_type="code"` (examples) — fixes symbol
  queries like `symbol="MLXModel"` returning irrelevant example code
- ChromaDB upsert, delete_by_content_type, and delete_by_source operations batched
  in chunks of 5,000 to avoid `BatchSizeExceededError` with large record counts
- Multi-concept query guidance added to tool descriptions and CLAUDE.md
- `_SERVER_VERSION` constant used in hub status test assertions (no more hardcoded
  version strings)

### Fixed

- Slug sanitization in source ingester matches `GitHubRepoIngester` — prevents
  silent skips for slugs with dots or special characters
- `content_type="code"` filter restored on path+line_start snippet mode —
  prevents returning source records when paths overlap
- Repo slug included in source chunk IDs — prevents cross-repo overwrites when
  repos share module names (forks)
- Import filter no longer hardcoded to "pipecat" — non-pipecat repos retain
  full API context
- Single-letter concepts (e.g. "C + concurrency") now decompose correctly
  (`MIN_CONCEPT_LENGTH` lowered from 2 to 1)

## [0.0.5] - 2026-02-28

### Added

- **Multi-concept query decomposition**: compound queries like
  "idle timeout + function calling + Gemini" now decompose into sub-concepts,
  run per-concept searches in parallel, and interleave results for balanced
  coverage. Use ` + ` or ` & ` as delimiters
- **RRF score normalization**: scores now divided by theoretical maximum so
  top results score ~1.0 instead of ~0.03 — evidence thresholds trigger
  correctly and confidence reports are meaningful
- **`imports` field on `ApiHit`**: `search_api` results include pipecat-internal
  imports for each module, enabling "what uses this class?" discovery
- `IndexStore.data_dir` property for clean index path access

### Changed

- `get_hub_status` only registered when `index_store` is provided — prevents
  broken MCP contract for old call sites
- `last_refresh_at` only written on fully successful refreshes (0 errors) —
  failed refreshes record `last_refresh_errored_at` instead
- Final reranked scores clamped to [0, 1] after heuristic adjustments
- Server instructions expanded with multi-concept query guidance
- License changed from MIT to BSD-2-Clause

### Fixed

- Multi-concept decomposition restricted to ` + ` and ` & ` delimiters only —
  comma and "and" caused false positives on natural language queries
- Ampersand delimiter requires surrounding spaces (`\s+&\s+`) — prevents
  splitting names like "AT&T"
- Ceiling division for per-concept candidate allocation — fixes under-allocation
  when limit isn't evenly divisible by concept count
- Round-trip imports in vector metadata reconstruction — `search_api` results
  from vector path no longer return empty imports
- `import json` moved to module level in `hybrid.py` — fixes potential
  `NameError` in conditional branch

## [0.0.4] - 2026-02-26

### Added

- **`get_hub_status` MCP tool** (7th tool): returns index health metadata —
  server version, last refresh timestamp, refresh duration, record counts by
  content type, distinct commit SHAs, and index data path
- **Persistent index metadata**: new `index_metadata` SQLite table stores
  key-value pairs (last refresh time, duration, record counts, error count)
  that survive across server restarts
- `FTSIndex.set_metadata()`, `get_metadata()`, `get_all_metadata()`,
  `get_index_stats()` methods for metadata CRUD and index statistics
- `IndexStore` proxies all metadata/stats methods to FTS backend
- New shared types: `GetHubStatusInput`, `HubStatusOutput`
- **`imports` field on `ApiHit`**: `search_api` results now include
  pipecat-internal imports for each module, enabling "what uses this class?"
  discovery
- **Pipecat imports persisted** in source `module_overview` chunks — filtered
  to `pipecat.*` imports only, stored in both FTS and ChromaDB backends

### Changed

- **Server instructions** expanded with tool routing guide — tells Claude
  which tool to use for each query pattern (conceptual → `search_docs`,
  examples → `search_examples`, API internals → `search_api`, etc.) and
  explicitly instructs "always use these tools instead of reading .venv"
- **Tool descriptions** rewritten to be action-oriented with use-case hints
  (e.g. `search_docs` now says "Use for 'how do I...?' questions")
- `create_server()` accepts optional `index_store` parameter for
  `get_hub_status` dispatch; tool is only listed when store is provided
- CLI `refresh` command now persists metadata after each successful run
  (failed refreshes record `last_refresh_errored_at` instead)
- CLI `serve` command passes `index_store` to `create_server`
- Single `_SERVER_VERSION` constant shared by server and handler
- `IndexStore.data_dir` property exposes index path without private access
- **RRF scores normalized to 0–1** — `reciprocal_rank_fusion()` now divides
  by theoretical maximum (`num_lists / (k + 1)`).  Top-ranked results that
  appear in both vector and keyword lists score 1.0 instead of ~0.03.
  Downstream evidence reports now correctly classify results as "strong" or
  "moderate" relevance instead of always reporting "low relevance"
- Final reranked scores clamped to [0, 1] after symbol boost / staleness
  penalty adjustments

## [0.0.3] - 2026-02-21

### Added

- **Source API ingester**: AST-based extraction of structured API metadata from
  the pipecat framework source (`src/pipecat/`). Produces three chunk types —
  module overview, class overview, and method/function — stored as
  `content_type="source"`. Extracts class names, base classes, decorators,
  method signatures with parameter types/defaults, return types, docstrings,
  and `@dataclass`/`@abstractmethod` detection (454 files, 5,075 chunks)
- New MCP tool `search_api` for searching framework internals (constructors,
  method signatures, frame types, processor APIs) with filters for `module`
  (prefix), `class_name`, `chunk_type` (`module_overview`, `class_overview`,
  `method`, `function`), and `is_dataclass`
- New shared types: `SearchApiInput`, `ApiHit`, `SearchApiOutput`
- `Retriever` protocol extended with `search_api` method
- ChromaDB and SQLite FTS5 index backends support new metadata fields:
  `module_path`, `class_name`, `chunk_type`, `base_classes`, `method_signature`,
  `is_dataclass`, `is_abstract`

### Fixed

- `build_signature()` no longer prepends `def name` — callers control the
  prefix, preventing doubled names in module/class overview chunks
- `_get_commit_sha()` now has `timeout=10` to prevent indefinite blocking
- `_make_chunk_id()` includes `line_start` to disambiguate duplicate
  class/method names within the same module (e.g. overloaded methods,
  re-opened classes in pipecat source)
- FTS `module_path` filter changed from exact-match to prefix-match, aligning
  with the vector backend and `search_api` contract
- mypy type narrowing for `kw_defaults[i]` in AST extractor (local variable
  assignment before None check)
- `base_classes` metadata stored as JSON string instead of comma-join,
  preventing corruption for generics like `Base[Foo, Bar]`
- `rel_path` in source ingester uses `as_posix()` for cross-platform
  compatibility (Windows backslashes no longer break module path derivation)
- `chunk_type` field description updated to include `'function'`

## [0.0.2] - 2026-02-21

### Added

- `PIPECAT_HUB_EXTRA_REPOS` environment variable for adding community repos
  without modifying source code (comma-separated slugs, appended to defaults
  with deduplication)
- CLI loads `.env` from the working directory on startup (explicit env vars
  take precedence)
- `.env.example` with documented usage
- Single-project repo ingestion: repos with no qualifying subdirectories
  (e.g. `src/`-layout packages) now fall back to treating the repo root as
  a single example — all code files are indexed recursively
- Root-level code file capture for Layout B repos: entry-point scripts
  (e.g. `sidekick.py`) sitting at the repo root are now indexed alongside
  subdirectory examples
- MCP server instructions (uv package manager guidance for LLM clients)

### Fixed

- `get_code_snippet` now accepts `intent` combined with `path` and
  `line_start` — `path` acts as an optional filter scoping the intent search
  to a specific file, and `line_start`/`line_end` trim results to the
  requested range
- Root-fallback repos (`src/`-layout) now get full taxonomy enrichment
  (`execution_mode`, `capability_tags`, `key_files`) — previously the
  taxonomy lookup keyed by `"."` missed, producing unenriched chunks that
  broke filtered retrieval (e.g. `execution_mode="local"` returned 0 hits)
- Root-level captured files (e.g. `sidekick.py` in Layout B repos) now
  inherit taxonomy metadata from a repo-root entry — previously the per-file
  lookup always missed, leaving chunks without `execution_mode`/`capability_tags`
- Root-fallback ingestion now skips `tests/`, `docs/`, `.github/`, and other
  non-source directories — previously `_iter_code_files` only skipped build
  artifacts, polluting example search with test and CI files.  The exclusion
  is applied only to the **first** path component relative to the scan root,
  so nested modules with the same name (e.g. `src/pkg/config/settings.py`)
  are preserved
- `.env` parser now correctly handles inline comments and quoted values —
  `KEY="val" # note` previously included `" # note` in the value, producing
  malformed repo slugs
- `HubConfig` import moved to top of `cli.py` (fixes E402 lint violation)
- Server version string corrected from `0.1.0` to match package version

## [0.0.1] - 2026-02-19

Initial release — local-first MCP server providing Pipecat docs and examples
context for Claude Code, Cursor, VS Code, and Zed.

### Added

- MCP server with stdio transport and 5 retrieval tools: `search_docs`,
  `get_doc`, `search_examples`, `get_example`, `get_code_snippet`
- Docs ingestion from `docs.pipecat.ai/llms-full.txt` (305 pages, 3,996 chunks)
- GitHub repo ingestion for `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`
  (729 code chunks)
- TaxonomyBuilder with automatic capability tag inference from directory names,
  README content, and Python imports/class references
- Hybrid retrieval: ChromaDB vector search + SQLite FTS5 keyword search with
  Reciprocal Rank Fusion reranking
- Local embeddings via `all-MiniLM-L6-v2` (sentence-transformers, no API key)
- `refresh` CLI command for full index rebuild with delete-before-ingest
  (stale records never persist across refreshes)
- Client config templates for Claude Code, Cursor, VS Code, and Zed
- Runtime warning when a discovered example dir has no taxonomy entry

### Fixed

- Mixed-layout repos (e.g. `examples/foundational/` + `examples/quickstart/`)
  get full taxonomy coverage — `TaxonomyBuilder.build_from_directory()` no longer
  short-circuits to foundational-only

### Known limitations

- Refresh always ingests from HEAD of configured repos — no version pinning
  (planned for v1)
- If ingestion fails after delete, that content type stays empty until next
  successful refresh (empty-on-failure policy; retain-previous-on-failure
  deferred to v1)
