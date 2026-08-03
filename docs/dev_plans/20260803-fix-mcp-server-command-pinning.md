# Task: Pin the Registered MCP Server Command to the Installed Interpreter

**Status**: Complete (unreleased) — both changes implemented; full suite green (1216 passed,
6 skipped), ruff + mypy + bandit clean. Verified against a live client: a seeded bare-name
entry is replaced with the interpreter command and connects, and a second run is a no-op.
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

**Codex needs no special case.** If `codex mcp get` does not exist it exits non-zero,
routing to the plain `add` path, which is exactly today's behaviour.

Replacement is not atomic: if `remove` succeeds and `add` fails, the entry is gone. That
is acceptable only because the branch is reached solely when the entry was already
wrong, which is why the no-op branch must never mis-fire on a correct registration.

This also corrects the reporting. `_register_with_cli` treats "already exists" as a
failure — its docstring claims a client that already has the server "says so and is not
an error", but `claude mcp add` exits 1.

## Files to modify

| File | Change |
|---|---|
| `src/pipecat_context_hub/cli_install.py` | `_server_command()` unconditional; `_register_with_cli()` converges; docstrings |
| `tests/` | see below |
| `CHANGELOG.md` | entry under **Fixed** |

`shutil` stays imported — `_detect_cli_clients()` still uses it.

## Tests

- `_server_command()` returns an absolute interpreter path **with** `pipecat-context-hub`
  on a monkeypatched `PATH` — the regression itself
- its result is independent of `PATH`, so registration never depends on the environment
- converge: stale entry → remove then add; matching entry → no-op; absent → plain add
- a client CLI with no `mcp get` falls back to plain `add`

## Verification

1. `pipecat context-hub install --print-config` with a venv active → absolute path
2. register in a temp project, then `claude mcp list` from a shell with no venv →
   `✔ Connected` (currently `ENOENT`)
3. re-run over a stale bare-name entry → rewritten and connecting
4. re-run over a correct entry → no-op, no config churn
5. `pipecat init` end to end with a venv active — the case that reproduced it

## Release

0.5.1. Consumers pin `pipecat-ai-context-hub>=0.5.1,<1`; the pipecat-side change is that
floor bump alone.

## Out of scope

- Chroma growth on repeated refresh (741 MB against 188 MB on a clean build) — real, but
  unrelated and separately measured.
- Scope selection for `mcp add`/`remove`. Both default to `local`, so they agree; whether
  a user-scope entry should be detected is a separate question.
- File-configured clients (Cursor, Windsurf, Zed) still receive a printed JSON block to
  paste. They inherit the corrected command; automating those writes is unchanged work.

## Open questions

- **Codex is unverified** — no `codex` on the development machine. The design degrades to
  current behaviour if `mcp get` is missing, but someone with it installed should confirm.
- **Advise instead of repair?** Change 2 could print "a different server is registered,
  run `claude mcp remove …`" rather than rewriting. No clobbering risk and no mutation
  driven by parsing, at the cost of leaving work to the user in a command whose purpose is
  to wire this up for them.
