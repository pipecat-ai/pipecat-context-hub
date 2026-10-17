# Config precedence: env vars, `.env`, and `config.toml`

Part of the [Pipecat Context Hub README](README.md). The variable list lives in the README's [Environment Variables](README.md#environment-variables) table.

Every entry point (the CLI, the `pipecat context-hub` Typer bridge, `serve`,
and the dashboard scripts) resolves `PIPECAT_HUB_*` settings from three
layers, first-writer-wins, in this order:

1. **Real environment variables** — highest precedence, e.g. exported from
   your shell profile or set in an MCP client's `env` block.
2. **`.env` in the current working directory** — project-local, loaded on
   every invocation from `Path.cwd()`.
3. **`~/.config/pipecat-context-hub/config.toml`** — machine-global, read
   only if present, for settings that should apply everywhere without being
   re-exported per shell or duplicated into every project's `.env`. Uses the
   same `PIPECAT_HUB_*` keys as flat TOML values (a homogeneous array of
   scalars, e.g. `PIPECAT_HUB_EXTRA_REPOS = ["org/repo-a", "org/repo-b"]`, is
   accepted and coerced to the same comma-separated form as the env-var
   form). See [`config.toml.example`](../config.toml.example).
   Keep it private — `chmod 600 ~/.config/pipecat-context-hub/config.toml`.
   Its values go straight into the process environment, so on POSIX systems a
   world-writable file, or one owned by another user, is ignored with a
   warning; a group-writable one is still loaded, but warned about.

Anything not set in any layer falls back to `HubConfig`'s field defaults
(the table above).

A project `.env` always outranks `config.toml` — that's true for every
invocation, not just a normal project-local CLI run. In particular, if an
MCP client happens to launch `serve` with its working directory set to a
project checkout that has its own `.env`, that `.env` shadows `config.toml`
for that session, the same as it would for a direct CLI invocation from that
directory. Whether a given MCP client passes a project `cwd` at all, or a
fixed/empty one, is client-specific and not something this project controls;
`serve` logs `cwd=... env_keys=...` at startup (`INFO` level) so you can
confirm which directory — and therefore which `.env`, if any — was in effect
for a given session. The path is home-redacted to `~/…`, and only key *names*
are logged, never values, so the line is safe to paste into a bug report.

Windows: the lookup path is `%USERPROFILE%\.config\pipecat-context-hub\config.toml`
in the common case, but that's a description of where `Path.home()` usually
resolves, not a separate rule — `Path.home()` reads `USERPROFILE`, falls back
to `HOMEDRIVE`+`HOMEPATH` if that's unset, and roaming or domain-managed
profiles can relocate the home directory entirely. This deliberately diverges
from this project's usual Windows convention for the index/repo cache
(`%LOCALAPPDATA%`) — `config.toml`'s location follows `Path.home() / ".config"`
on every platform, matching its POSIX/macOS path structure rather than
Windows convention, so the same relative path (`.config/pipecat-context-hub/config.toml`)
works everywhere `Path.home()` resolves.
