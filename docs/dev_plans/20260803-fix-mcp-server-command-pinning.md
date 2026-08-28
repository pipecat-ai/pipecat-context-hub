# Task: Pin the Registered MCP Server Command to the Installed Interpreter

**Status**: Complete (unreleased) — all three original changes implemented, plus a
rollback-safety/repair-logic hardening pass driven by three rounds of adversarial Codex
review and xhigh multi-agent code review (see "Post-review hardening" below). Full suite
green (1232 passed, 6 skipped), ruff + mypy + bandit clean. Verified against a live
client: a seeded bare-name entry is replaced with the interpreter command and connects,
and a second run is a no-op.
**Date**: 2026-08-03
**Branch**: `fix/mcp-server-command-pinning`
**Follows**: [`20260725-feature-pipecat-cli-plugin.md`](20260725-feature-pipecat-cli-plugin.md) — the CLI bridge whose distribution change exposed this.

## Motivation

`install` registers a server that a client cannot always start. Reproduced end to end
on macOS with `pipecat-ai[cli]` installed as a `uv tool`:

```
$ cd $(mktemp -d) && pipecat init .
✓ Registered the Context Hub MCP server

$ claude mcp list                          # same shell, project venv still active
pipecat-context-hub: pipecat-context-hub serve - ✔ Connected

$ env PATH=/usr/bin:/bin:$HOME/.local/bin claude mcp list      # any other shell
pipecat-context-hub: pipecat-context-hub serve
  - ✘ Failed to connect — ENOENT: Executable not found in $PATH: "pipecat-context-hub"
```

Nothing reports this to the user. Claude Code does not announce a server that failed
to start, so the agent simply has no Pipecat tools and answers from training data —
the exact failure the generated `AGENTS.md` exists to prevent — while `pipecat init`
has already printed a checkmark.

### Root cause

`_server_command()` prefers a bare console-script name whenever one is on `PATH`:

```python
if shutil.which(_SERVER_NAME):
    return [_SERVER_NAME, "serve"]
return [sys.executable, "-m", "pipecat_context_hub", "serve"]
```

Two distinct problems.

**The probe and the registration answer different questions.** `shutil.which` asks
"is there a hub on *this* `PATH`, *now*?" The config asserts "there will be a hub on
the *client's* `PATH`, at *session start*." The second does not follow from the first —
different process, later, often launched from a GUI with no shell `PATH` at all. The
path `which` finds is then discarded, so even the evidence gathered goes unused.

**The branch inverted when the hub's distribution changed.** It was written when
standalone was the normal install, which does expose `pipecat-context-hub` on `PATH`;
the interpreter branch was the fallback for the `--with` co-install, where the hub is
importable but its script is not exposed. Since the hub became a dependency of
`pipecat-ai[cli]`, that co-install no longer exists and `uv tool` exposes only the host
package's scripts — so the *fallback* is now correct for the primary install, and the
*preferred* branch fires only when an unrelated hub is on `PATH`:

| Install | script on `PATH`? | branch | correct? |
|---|---|---|---|
| `uv tool install "pipecat-ai[cli]"` | no — uv exposes only host scripts | fallback | yes |
| any active venv carrying `pipecat-ai[cli]` | yes | preferred | **no** |
| leftover standalone install | yes | preferred | **no** |

The venv row is not exotic: `pipecat-ai[evals]` depends on `pipecat-ai[cli]`, so every
project scaffolded with `--eval` puts `pipecat-context-hub` in its `.venv/bin`. Having
one active while running `pipecat init` is the ordinary state of working on a bot.

The docstring's own closing argument — *"naming the interpreter also pins the client to
the version that is installed"* — is the reason to do that unconditionally.

## Approach (decided with maintainer)

### 1. Always name the interpreter

```python
def _server_command() -> list[str]:
    return [sys.executable, "-m", "pipecat_context_hub", "serve"]
```

Correct in both directions: run as `pipecat context-hub install` it pins the CLI's
bundled hub; run as `pipecat-context-hub install` it pins that standalone. You get the
hub you invoked, at an absolute path, resolved once at install time.

All four client paths read this single value, so `claude-code`, `codex`, and the printed
JSON for Cursor/Windsurf/Zed are fixed together.

### 2. Make re-registration converge

Alone, (1) reaches only brand-new projects. `claude mcp add` exits 1 on an existing
name and leaves the entry untouched, so every registration already written stays broken
permanently and re-running `install` changes nothing.

```
mcp get <name>
├─ non-zero  → absent, or the client CLI has no `get` → mcp add   [current behaviour]
├─ names our interpreter and module → no-op, "already configured"
└─ otherwise → mcp remove (tolerating exit 1), then mcp add
```

Two deliberate properties:

**Detection matches our own command rather than parsing theirs.** `claude mcp get`
prints human-readable text; we ask only whether the entry already invokes *this*
interpreter and module. If that output format changes we fail to recognise our own
entry and rewrite it — a redundant write, never a broken server.

**Codex needs no special case**, and this is what matching our own command buys. `codex
mcp get` exists and prints a layout unrelated to Claude Code's — lowercase `command:` /
`args:` against `Command:` / `Args:` — yet both converge, because the check never parses
either. A client CLI with no `mcp get` at all exits non-zero and routes to the plain `add`
path, which is the prior behaviour.

The two also scope differently: Claude Code registers per project, codex globally. So a
second project legitimately re-registers with Claude Code while codex reports no change.

### 3. Report whether anything was configured

`install` exited `0` both when it registered the server and when it only printed the
config to paste, so a caller cannot tell the two apart. A wrapper that captures the
output — `pipecat init` does, to keep its own output to one line — then reports a
registration that never happened and swallows the block the user needed.

It now exits `3` when nothing was configured automatically: the path every
file-configured editor (Cursor, VS Code, Zed) takes, and any machine with no client CLI
installed. `1` still means a client CLI rejected the registration.

Exit codes are already this CLI's machine-readable outcome channel — the main group
documents `0`/`1`/`2` for the query commands — so collapsing two materially different
outcomes into `0` was inconsistent with the hub's own convention, independent of who
calls it.

### Notes on (2)

Replacement is not atomic: if `remove` succeeds and `add` fails, the entry is gone. That
is acceptable only because the branch is reached solely when the entry was already
wrong, which is why the no-op branch must never mis-fire on a correct registration.

This also corrects the reporting. `_register_with_cli` treats "already exists" as a
failure — its docstring claims a client that already has the server "says so and is not
an error", but `claude mcp add` exits 1.

**This "acceptable" call above turned out not to be** — see "Post-review hardening" below.
Losing a registration silently (not just leaving it as it was) is a materially worse
outcome than the failure this plan set out to fix, so three review rounds after the
above was implemented, it was made rollback-safe instead.

## Post-review hardening (adversarial + xhigh code review)

Three rounds of Codex adversarial review and xhigh multi-agent code review, run after
the approach above was implemented, found progressively deeper problems in exactly the
non-atomic remove-then-add repair this plan flagged as "acceptable." Each was fixed and
re-verified (full suite + ruff + mypy) before moving to the next round. Commits, in order:

1. **`3159574`** — the repair path removed an existing registration and ignored the
   removal's success before attempting the replacement; a failed replace after a
   successful remove permanently lost a working registration. Fixed: fail closed unless
   `mcp get` explicitly reports the entry missing; capture the exact Claude registration
   before removal and restore it via `mcp add-json` if replacement fails; use Codex's
   atomic overwrite instead of remove-then-add.
2. **`ecc4337`** — the round-1 fix introduced its own bug: an unanchored regex could
   match a scope-shaped substring inside a registration's `Args:` line ahead of the real
   removal-instruction line, misdirecting repair to the wrong Claude scope. Fixed:
   anchor the regex to the literal instruction line.
3. **`9f28c63`** — the fail-closed inspection from round 1 aborted `install` on *any*
   unrecognized `mcp get` failure, even on a fresh machine with nothing to protect.
   Rollback failure was also indistinguishable from success, one repair branch skipped
   rollback entirely, and the destructive `mcp remove` ran with no transcript echo.
   Fixed: split "get itself failed" (safe to fall through to a plain add) from
   "something exists but can't be parsed" (fail closed); thread rollback outcome through
   a three-way `Literal["ok", "failed", "corrupted"]` return from `_register_with_cli`.
4. **`56f543d`** — `"corrupted"` (registration lost, unrecoverable) and `"failed"`
   (registration intact) both exited `1`, indistinguishable to a scripted caller. Added
   `_EXIT_REGISTRATION_LOST = 4`.
5. **`da3351f`** — a Codex transport value could crash `install` with an uncaught
   `AttributeError`; an ambiguous `mcp get` failure skipped the remove-before-add safety
   net the original repair path always applied; `_claude_config`'s local-scope lookup
   `KeyError`'d on legitimate project paths differing only by symlink resolution; a
   non-zero (vs. `None`) `mcp remove` result skipped rollback.
6. **`1bca555`** — round 3 found that `da3351f`'s "ambiguous state" repair (a
   destructive, unscoped `mcp remove` with no captured rollback state) could still
   delete a working registration and misreport it as `"failed"` (implying it survived)
   instead of `"corrupted"`. Three rounds in a row found a new bug in this exact corner,
   so simplified instead of patching further: fold "ambiguous" back into "error" — fail
   closed, no destructive remove attempted, matching every other unparseable-inspection
   case in this module. Also fixed `_config_matches_command` crashing on an explicit
   `"args": null`, and one stale/unreachable project path entry aborting the entire
   local-scope lookup instead of being skipped.
7. **`63531a9`** — bandit's Security CI job flagged the three `assert` statements added
   by the rollback-safety fixes (B101). Suppressed with `# nosec B101` and a comment
   documenting the invariant each guards, matching this file's existing suppression
   convention.

Net effect on the design above: (2)'s repair is no longer "acceptable to lose the entry
on a failed replace" — it is rollback-safe for Claude Code (capture-before-remove,
restore-on-failure) and atomic for Codex (native overwrite), with a dedicated exit code
for the one case that still can't recover (rollback itself also fails).

## Files to modify

| File | Change |
|---|---|
| `src/pipecat_context_hub/cli_install.py` | `_server_command()` unconditional; `_register_with_cli()` converges; docstrings; post-review: `_inspect_registration()`/`_Registration` state machine (`absent`/`matching`/`mismatched`/`unknown`/`error`), `_claude_config()` + `_restore_claude_registration()` for rollback capture/restore, `_fail_with_rollback()`/`_first_error_line()`/`_config_matches_command()` helpers, `_EXIT_REGISTRATION_LOST` |
| `tests/unit/test_cli_install.py` | see below |
| `CHANGELOG.md` | entry under **Fixed** |

`shutil` stays imported — `_detect_cli_clients()` still uses it.

## Tests

- `_server_command()` returns an absolute interpreter path **with** `pipecat-context-hub`
  on a monkeypatched `PATH` — the regression itself
- its result is independent of `PATH`, so registration never depends on the environment
- converge: stale entry → remove then add; matching entry → no-op; absent → plain add
- a client CLI with no `mcp get` falls back to plain `add`
- post-review: rollback-on-failed-replace (add fails after a successful remove restores
  the captured entry), `mcp get` timeout falls through to a plain non-destructive add,
  anchored Claude scope-parsing regression (misleading `-s user` fragment in an `Args:`
  line ahead of the real instruction), corrupted-vs-failed exit code (`4` vs `1`),
  Codex non-dict transport guard, `_claude_config`'s real file-parsing logic directly
  (project/user/local/symlinked-local scopes — previously only exercised through mocks),
  `_config_matches_command` on an explicit `"args": null`

## Verification

1. `pipecat context-hub install --print-config` with a venv active → absolute path
2. register in a temp project, then `claude mcp list` from a shell with no venv →
   `✔ Connected` (currently `ENOENT`)
3. re-run over a stale bare-name entry → rewritten and connecting
4. re-run over a correct entry → no-op, no config churn
5. `pipecat init` end to end with a venv active — the case that reproduced it
6. both client CLIs: `claude` and `codex` register, and each reports no change on a
   re-run against its own config (per project for Claude Code, global for codex)

## Release

0.5.1. Consumers pin `pipecat-ai-context-hub>=0.5.1,<1`; the pipecat-side change is that
floor bump alone.

## Out of scope

- Chroma growth on repeated refresh (741 MB against 188 MB on a clean build) — real, but
  unrelated and separately measured.
- Scope selection for `mcp add`/`remove`. Both default to `local`, so they agree; whether
  a user-scope entry should be detected is a separate question.
  **Resolved (2026-08-27, PR #121):** a fresh Claude registration is now made at `user`
  scope so it covers every directory; a mismatched entry is still removed and re-added at
  the scope it already holds, which is what keeps `add`/`remove` agreeing. User-scope
  entries were already detected — `_inspect_registration`'s scope regex and
  `_claude_config` both handle `user` — so only the fresh-entry default changed.
- File-configured clients (Cursor, Windsurf, Zed) still receive a printed JSON block to
  paste. They inherit the corrected command; automating those writes is unchanged work.

## Open questions

- **The paste-and-restart round trip is unverified.** The config printed for Cursor,
  VS Code, and Zed is exercised only as far as its content; nobody has pasted it into one
  of those editors and watched the server come up.
- **Advise instead of repair?** Change 2 could print "a different server is registered,
  run `claude mcp remove …`" rather than rewriting. No clobbering risk and no mutation
  driven by parsing, at the cost of leaving work to the user in a command whose purpose is
  to wire this up for them.
