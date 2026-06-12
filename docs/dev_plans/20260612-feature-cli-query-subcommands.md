# Task: One-Shot CLI Query Subcommands (every MCP tool as a shell command)

**Status**: Complete
**Component**: cli
**Assigned to**: markbackman
**Priority**: Medium
**Branch**: feat/cli-query-subcommands
**Created**: 2026-06-12
**Completed**: 2026-06-12

## Objective

Expose every MCP tool as a one-shot CLI subcommand that reuses the *same* tool
handlers the MCP server dispatches, so an agent can query the local index
without a warm MCP session. This plan documents the **intended design** and
diffs it against the as-built PR #75 implementation, then drives the gaps
(home-path redaction parity, per-command dispatch test coverage) to closure.

## Context

PR #75 (`feat/cli-query-subcommands`) adds a second "front door" to the local
index: `pipecat-context-hub <tool>` subcommands that print JSON on stdout and
logs on stderr. MCP clients only load servers at session start, so a one-shot
CLI is the bootstrap path (`uvx … check-deprecation PipelineTask`) before a
session is warm. The feature shipped green, but a deep review (2026-06-12) and a
fresh-context codebase exploration found two divergences from the project's own
conventions that this plan closes. No dev plan existed for the feature; this is
a reverse-engineered spec used as the conformance baseline.

## Requirements

- **R1 — Handler parity.** Each subcommand dispatches the *same* handler
  function `server/main.py` uses for the corresponding MCP tool; no divergent
  reimplementation of retrieval logic.
- **R2 — Tool↔command coverage.** Every MCP tool has exactly one CLI command; a
  parity test fails if a tool gains no command (drift guard).
- **R3 — I/O contract.** JSON result on stdout, human/log output on stderr.
- **R4 — Exit codes.** `0` success / `1` invalid input / `2` index
  missing-or-empty, the `2` path matching `serve`'s `_EXIT_INDEX_UNREADY`.
- **R5 — Model-load gating.** Lookup-only commands (`check-deprecation`,
  `status`, `get-doc`, `get-example`) skip embedding/reranker construction;
  semantic commands (`search-docs`, `search-api`, `search-examples`,
  `get-code-snippet`) load them.
- **R6 — Home-path redaction.** Any absolute path in *user-facing stderr* is
  home-redacted to `~/…`, matching the convention `cli.py` already applies to
  its startup banner and reranker-cache warning (`_redact_home`). Routine bug
  reports must not leak the local filesystem layout / username. **This includes
  paths embedded inside an exception message** (`{exc}`), not just a directly
  interpolated `data_dir` token — a `FileNotFoundError` or
  `IncompatibleIndexFormatError` renders the absolute path inside its own
  `__str__`, so redaction must operate on the *composed* stderr string, not a
  single argument. The same leak exists on the `serve` front door
  (`cli.py:271`, `logger.error("%s", exc)`); R6 covers both for parity.
- **R7 — Per-command dispatch tests.** Each subcommand has a test asserting it
  invokes the correct handler and returns the expected exit code, so a
  mis-wired handler cannot pass CI.

## Conformance Matrix (intended design vs as-built PR #75)

Evidence from the as-built tree at `feat/cli-query-subcommands`
(`95e2d37`). ✅ Met / ⚠️ Gap.

| Req | Status | Evidence |
|-----|--------|----------|
| R1 handler parity | ✅ | `cli_query.py:203-236` `_dispatch()` imports + calls the same `server/tools/*` handlers as `server/main.py:251-293`; identical import lists. |
| R2 tool↔command coverage | ✅ | `_TOOL_TO_COMMAND` (`cli_query.py:62-71`) maps all 8 tools; `test_cli_query.py:36-39` asserts `tool_names == set(_TOOL_TO_COMMAND)`. |
| R3 JSON/stderr split | ✅ | result JSON to stdout; errors via stderr in `_query_runtime` (`cli_query.py:158-175`). |
| R4 exit codes | ✅ | `_EXIT_INDEX_UNREADY` parity asserted `test_cli_query.py:44-46`; bad-input → 1 (`test_cli_query.py:222-234`). |
| R5 model-load gating | ✅ | `needs_embeddings` gate `cli_query.py:139-200`; lookup cmds pass `False` (`:302,:369,:491,:502`), semantic pass `True` (`:285,:355,:419,:477`). |
| **R6 home-path redaction** | ✅ **Met** (was GAP; fixed `90688d1` + deep-review `dc051c9`) | `shared/paths.py::redact_home_in_text` redacts the whole composed message at all three `cli_query.py` stderr sites and the `serve`/`refresh` error branches in `cli.py`, so a `{exc}`-embedded `chroma_path` no longer leaks. (As-built-at-creation gap was: three unredacted stderr strings + the symmetric `serve` leak.) |
| **R7 per-command dispatch tests** | ✅ **Met** (was GAP; fixed `c210320`) | `TestDispatch` now covers all 8 subcommands; lookup cmds assert no `EmbeddingService` construction and `get-example --no-readme` inversion is pinned. (As-built-at-creation gap was: only `check-deprecation`, `status`, `search-docs` covered.) |

Out of scope (Low, documented for follow-up, not fixed here): `get-code-snippet`
declares `needs_embeddings=True` but its path+line-range mode is FTS-only
(~2s wasted model load); `_invoke` silently drops empty multi-value options
(`--tag ()`).

## Review Focus

- **Circular import.** `cli.py:22` imports `register_query_commands` from
  `cli_query`, so `cli_query` must NOT import the redaction helper back from
  `cli.py`. The fix moves it to a shared module imported by both.
- **Mandatory back-compat alias.** `tests/unit/test_cli.py:16` imports
  `_redact_home` from `cli.py` and exercises it (`test_cli.py:108-133`). After
  the move, `cli.py` MUST keep `_redact_home = redact_home` re-exported or that
  suite breaks at import time. This is not optional.
- **Redaction completeness (text-aware).** The leak is NOT confined to the
  `data_dir` token — `{exc}` renderings embed absolute paths too. Redaction must
  apply to the *whole composed stderr message* at all three sites
  (`cli_query.py:158,:162,:171`). A grep for `data_dir` alone misses `:158` and
  the `{exc}` half of `:162`. The completeness criterion is "every `err=True`
  stderr emission, including `{exc}` renderings," not "every `data_dir` token."
- **Lookup-path safety.** The new dispatch tests must exercise lookup commands
  end-to-end with `needs_embeddings=False` and assert no `EmbeddingService` is
  constructed (guards the `HybridRetriever(embedding_svc=None)` path).
- **Test arrangement (avoid vacuous pass).** Redaction tests must patch
  `Path.home()` to a temp dir AND set the index `data_dir` *under* that home,
  else `redact_home` is a no-op and the assertion passes without exercising
  redaction. The two stderr branches fire on different triggers: `:162` on a
  generic `Exception`/`FileNotFoundError` opening the index, `:171` on an empty
  (total=0) index — they need separate tests.
- Behavior-preserving: redaction only changes stderr text; JSON stdout and exit
  codes are unchanged.

## Implementation Checklist

### Phase 1: Home-path redaction parity (R6)

**Impl files:** `src/pipecat_context_hub/shared/paths.py, src/pipecat_context_hub/cli.py, src/pipecat_context_hub/cli_query.py`
**Test files:** `tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_cli.py tests/unit/test_cli_query.py -v`
**Validation cmd:** `uv run ruff check src/ tests/ && uv run mypy src/ tests/`

- Create `shared/paths.py` with TWO helpers:
  - `redact_home(path) -> str` — moved verbatim from `cli.py:37-51` (prefix
    match: the whole arg is a path). Serves the banner call sites.
  - `redact_home_in_text(text) -> str` — NEW. Replaces every occurrence of
    `str(Path.home())` (exact and `home + os.sep` forms) with `~` *inside* an
    arbitrary message string, so an absolute path embedded mid-message (e.g. in
    a `{exc}` rendering) is redacted. Guard against partial-username
    over-match (replace `home + sep` and exact-`== home`, not a raw substring).
- In `cli.py`: import both helpers; **keep `_redact_home = redact_home`
  re-exported** (mandatory — `test_cli.py:16` imports it); preserve call sites
  `:316`, `:367`; wrap the `serve` error rendering at `cli.py:271`
  (`logger.error("%s", exc)`) with `redact_home_in_text` for front-door parity.
- In `cli_query.py`: import `redact_home_in_text`; wrap the **whole composed
  message** at all three stderr sites — `:158` (`f"Error: {exc}"`), `:162`
  (`f"...failed to open index at {data_dir}: {exc}"`), `:171`
  (`f"...index at {data_dir} is empty"`). Confirm by re-reading the module that
  no other `err=True` emission carries an unredacted path.
- Add **two** redaction tests (one per branch). Each patches `Path.home()` to a
  tmp dir and sets the index `data_dir` under it, then asserts the stderr
  contains `~/` and does NOT contain `str(home)`:
  - `:162` branch — trigger a generic `Exception`/`FileNotFoundError` on index
    open; assert the `{exc}`-embedded path is redacted too (not just `data_dir`).
  - `:171` branch — empty index (`total=0`).

### Phase 2: Per-command dispatch test coverage (R7)

**Impl files:** `tests/unit/test_cli_query.py`
**Test files:** `tests/unit/test_cli_query.py`
**Test command:** `uv run pytest tests/unit/test_cli_query.py -v`
**Validation cmd:** `uv run ruff check tests/`

- Add a `TestDispatch` case per untested command — `get-doc`, `get-example`,
  `search-examples`, `get-code-snippet`, `search-api` — asserting `exit_code == 0`
  and that the correct handler from `server/tools/*` is invoked (mock/patch the
  handler and assert called once with the expected args shape). Name the shape
  per command: at least the query/id arg + one flag.
- For `get-example`, explicitly assert `include_readme is False` when
  `--no-readme` is passed (the command maps `--no-readme` → `include_readme=not
  no_readme` at `cli_query.py:368` — an inversion worth pinning).
- For the lookup commands among these (`get-doc`, `get-example`), assert
  `EmbeddingService` is NOT constructed (patch with `side_effect=AssertionError`,
  reusing the `test_check_deprecation_clean_symbol` pattern at
  `test_cli_query.py:75-90`), locking the `needs_embeddings=False` contract.

## Technical Specifications

### Files to Modify
- `src/pipecat_context_hub/cli.py` — replace local `_redact_home` def with
  imports of `redact_home` + `redact_home_in_text` from `shared/paths.py`; keep
  `_redact_home = redact_home` alias (test_cli.py depends on it); keep call
  sites `:316`, `:367`; wrap `serve` exc rendering at `:271` with
  `redact_home_in_text`.
- `src/pipecat_context_hub/cli_query.py` — import `redact_home_in_text`; wrap the
  whole composed stderr message at `:158`, `:162`, `:171`.
- `tests/unit/test_cli_query.py` — 2 redaction tests + 5 dispatch tests
  (incl. `get-example --no-readme` negation + lookup-cmd no-embedding asserts).

### New Files to Create
- `src/pipecat_context_hub/shared/paths.py` — `redact_home(path) -> str` (moved
  verbatim, prefix match) + `redact_home_in_text(text) -> str` (NEW, redacts
  home occurrences embedded anywhere in a message string).

### Architecture Decisions
- **Shared module, not cross-import.** Redaction helpers live in
  `shared/paths.py` because `cli.py` already imports `cli_query` (`cli.py:22`);
  importing back would be a cycle. `shared/` peers (`model_loading`, `tracking`)
  import neither CLI module, so the layer stays leaf-level.
- **Two functions, not one.** `redact_home` (whole-arg-is-a-path, prefix match)
  serves the banner sites unchanged; `redact_home_in_text` (substring-aware)
  serves composed messages where a path is embedded mid-string via `{exc}`. A
  prefix-only helper cannot redact `"Error: ... '/Users/x/.../chroma.sqlite3'"`
  because the string starts with `"Error:"`, not the home path — this is the
  defect the review caught.
- **Fix at the emission site, not `errors.py`.** Redacting `chroma_path` inside
  `IncompatibleIndexFormatError.__str__` would miss the `:162` branch, which
  fires on a *generic* `Exception`/`FileNotFoundError` whose own message embeds
  the path. Text-aware redaction at each emission site covers every exception
  type; an `errors.py`-only fix does not.
- **Mandatory back-compat alias.** `cli.py` MUST keep `_redact_home =
  redact_home` — `test_cli.py:16` imports the underscored name. Without it
  Phase 1 breaks an existing suite at import time; hence Phase 1's test command
  includes `test_cli.py`.
- **Scope discipline.** Only R6/R7 gaps are fixed (R6 now correctly scoped to
  all three sites + `serve` parity). The two Low items stay out-of-scope.

### Dependencies
- No new dependencies. `click` is the CLI framework (`pyproject.toml:22`,
  `click>=8.0,<9.0`).

## Testing Notes

### Test Approach
- [ ] Unit: redaction of `data_dir` in both stderr error paths (patched home)
- [ ] Unit: per-command dispatch for the 5 untested commands
- [ ] Unit: lookup commands construct no `EmbeddingService`

### Test Results
- [ ] All existing tests pass
- [ ] New tests added and passing
- [ ] `ruff check` + `mypy` clean

## Acceptance Criteria

- R6: none of the three `cli_query.py` stderr sites (`:158,:162,:171`) leak the
  literal home dir — including paths inside `{exc}` renderings — verified by two
  branch tests; `serve`'s `cli.py:271` is redacted for parity; redaction helpers
  live in `shared/paths.py`; `_redact_home = redact_home` alias preserved so
  `test_cli.py` stays green.
- R7: every one of the 8 subcommands has a per-command dispatch test; lookup
  commands assert no embedding construction; `get-example --no-readme` inversion
  pinned.
- Full suite green (`uv run pytest tests/ -v`); `ruff` + `mypy` clean.
- Conformance matrix R6/R7 flip to ✅ with updated evidence.

## Final Results

Both gaps closed via `/conduct` (2026-06-12), each a clean-context subagent phase:

- **Phase 1 (R6, commit `90688d1`)** — new `shared/paths.py` with `redact_home`
  (moved) + `redact_home_in_text` (NEW, redacts paths embedded mid-message).
  All three `cli_query.py` stderr sites and the `serve` site (`cli.py:271`) now
  redact the *whole composed message*, closing the `{exc}`-embedded-path leak
  the review caught. `_redact_home = redact_home` alias kept (test_cli.py green).
  Two branch tests pin redaction without vacuous pass. Edge cases verified:
  embedded path → `~/…`; sibling `/Users/<user>ier` left intact (no over-match).
- **Phase 2 (R7, commit `c210320`)** — dispatch tests for the 5 untested
  subcommands; all 8 now covered. Lookup cmds assert no `EmbeddingService`;
  `get-example --no-readme` inversion pinned.

Conformance: R6 and R7 now **Met**. Final parity gate: full suite **1096 passed,
6 skipped**, `ruff` + `mypy` clean.

Follow-up (still out of scope, documented for later): `get-code-snippet`
`needs_embeddings=True` despite FTS-only path mode; `_invoke` dropping empty
multi-value options.

<!-- reviewed: 2026-06-12 @ 7966b8bb3f5a82b5cb7ca47a4390fa20566980f1 -->

## Progress

- [x] Phase 1: Home-path redaction parity (R6) — commit 90688d1; 76 tests pass, ruff+mypy clean
- [x] Phase 2: Per-command dispatch test coverage (R7) — commit c210320; 20 dispatch tests, ruff clean

## Findings

(durable notes land here during /conduct)

## Issues & Solutions

(problems encountered and resolutions)
