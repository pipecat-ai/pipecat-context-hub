---
name: setup
description: Set up the Pipecat Context Hub plugin after an explicit request, checking retrieval and required Pipecat/Cloud CLIs, installing missing prerequisites, configuring an initial index/MCP and guiding Cloud account signup and login when authorised.
---

Use this workflow when the user requests plugin, Context Hub or Pipecat/Cloud
CLI setup. Determine the requested capabilities from the conversation: full
plugin setup includes the build/deploy toolchain; explicitly retrieval-only
setup needs only PCH. PCH CLI is a retrieval backup, while Pipecat CLI and its
Cloud extension are required tools for Build and Deploy respectively.
Plugin installation
or an exploration question alone does not authorise dependency installation,
index creation or host configuration. PCH is one Python package providing both
the MCP server and query CLI; users do not need two separate downloads.
Skills supply instructions, not execution permissions or installation hooks.

## Inspect and propose setup

1. Identify the execution environment and selected client. Check native shell/file
   access with a harmless command. A local computer or cloud execution environment
   can run PCH when it supports Python and subprocesses; installing a plugin on
   the web does not provision that environment. Record whether its runtime/index
   will persist between tasks. Do not assume cloud files configure a local client.
2. If the packaged `pipecat-context-hub-chatgpt-plugin` connection works, call
   `get_hub_status`. Discover existing CLI retrieval using
   [Explore's CLI backup](../explore/SKILL.md#cli-backup): verified
   `pipecat context-hub` or standalone `pipecat-context-hub`, supported help and
   `status`. Keep stdout JSON separate from stderr and inspect actual exit errors;
   exit 2 alone is not proof of an empty index. Record versions, index path, count,
   refresh date, framework pin and indexed framework version. Compare both routes
   when available; do not silently select a different index or installation.
   Separately inspect `pipecat --version`, `pipecat init --help` and, for Cloud,
   `pipecat cloud --help`, `pipecat cloud auth login --help` and organisation-list
   help. A listed `cloud` command may be an unavailable-extension stub; require
   working subcommand help rather than treating the root listing as readiness.
3. Reuse working PCH and a healthy populated index. Staleness, a different requested
   framework version or disabled reranking does not authorise refresh, upgrade or
   model downloads. A corrupt/unreadable existing index is a separate recovery
   request; stop that branch without reset, repair, deletion or a replacement index.
   If working MCP needs no local setup and native execution is absent, report MCP
   usable and CLI backup unavailable rather than installing an execution adapter.
4. Present the concrete changes needed: required PCH/Pipecat/Cloud package/version
   specifications and retained extensions, tool-manager
   environment, resolved initial-index path and sources/framework pin, selected
   client and plugin configuration destination, plus any existing-file backup.
   Explain that first indexing downloads models and source repositories and can
   take several minutes. Use only already-authorised changes. If the user has
   explicitly requested installation, initial indexing and configuration, proceed
   within that scope; otherwise obtain one setup approval covering the proposed
   changes before executing them. A readiness-only request stays read-only.

## Required workflow CLIs

| Requested workflow | Required tools | Account requirement |
|---|---|---|
| Explore | Usable PCH MCP or query CLI | No Cloud account |
| Build | Pipecat CLI with working `init`, plus PCH retrieval | No Cloud account |
| Deploy | Pipecat CLI with working Cloud extension, plus PCH retrieval | Authenticated Pipecat Cloud account and selected workspace/organisation |

For authorised fresh full-plugin or Cloud setup, the
[documented installation](https://docs.pipecat.ai/api-reference/cli/overview#installation)
is `uv tool install "pipecat-ai[cli]" --with pipecatcloud`. This supplies the
Pipecat CLI with its Cloud extension; current distributions also bundle PCH.
For an explicitly local-build-only setup, `uv tool install "pipecat-ai[cli]"`
is sufficient. Cloud deployment cannot replace this CLI with PCH's MCP tools.

Reuse existing working CLI installations. Inspect tool-manager/package metadata
before modifying an existing environment: `uv tool install --with` replaces the
tool environment, so retain every existing selected extension and its constraints
in the proposed command. Do not silently remove extensions, change pins or
upgrade a working installation just to unify it with PCH. An authorised separate
environment is an alternative when the current environment must be preserved.
If `uv` is unavailable, use the approved virtual-environment route below with
the required packages, or report the missing execution prerequisite.

Verify actual installed versions and required subcommand help after installation.
If `pipecat context-hub --help` works, use that PCH installation; otherwise retain
or install standalone PCH separately. A working Pipecat/Cloud CLI is mandatory
for the requested Build/Deploy capability even when remote MCP retrieval works.

## Install missing PCH and create the initial index

Inspect the installed tool manager's help. When `uv` is already available, the
standalone package install is `uv tool install pipecat-ai-context-hub`; use an
explicit `==VERSION` only when selected by the user. This is sufficient for both
MCP and CLI. PCH may already be bundled with the installed Pipecat CLI; do not
install a duplicate or upgrade a working CLI to obtain it. If `uv` is absent,
an authorised compatible Python virtual environment and its `pip` can install
the same package. Do not install globally or bootstrap another tool manager
without including that in the approved setup. Installed package metadata controls
Python requirements; this repository currently requires Python 3.11 or newer.

Discover the resulting executable and its actual Python environment from native
tool-manager metadata. Inspect PCH help again. Use a neutral working directory
outside projects so unrelated project `.env` files do not select setup inputs.
Retain explicitly selected Hub environment/global configuration without exposing
credentials. Check the configured data directory and source list before writes;
private/custom source indexing requires explicit authorisation.

Only create an initial index when the resolved directory is absent or status
and directory inspection establish an empty, uninitialised index. Use the
verified PCH prefix with `refresh`, honouring the selected/configured framework
pin and supported flags. Never add `--force`, `--reset-index` or `--prune` to
initial setup. Do not refresh a populated index to test setup. On failure, retain
partial state and report the bounded error; do not retry, clear it or redirect
to another index automatically. Check `status` after initial indexing and
require nonzero records with known provenance before calling retrieval ready.

## Configure only the selected plugin connection

Preserve a working packaged connection. If it needs binding, verify the selected
Hub Python can import `pipecat_context_hub` with `-P` from the neutral directory.
Keep the virtual-environment interpreter path without resolving its symlink out
of that environment. `install --print-config`, when supported, is a read-only
diagnostic; its standalone server name is not the packaged connection identity.
Do not run bare `install`: it registers all detected clients and refreshes.

Use the packaged [local renderer](../../scripts/prepare_local.py) with that
verified Hub Python and `-P`. Prepare into an absent/empty directory outside the
checkout and installed plugin cache. Discover the selected marketplace's actual
source and the host's supported update commands; preserve the plugin/marketplace
identity. If replacing an existing source copy, prepare and validate a fresh
copy first, preserve the old directory as a recoverable backup, then update only
that selected source and reinstall/refresh through the supported host flow.
Never edit the installed cache or unrelated client/MCP entries.

Inspect generated `mcp.json`: the only server is
`pipecat-context-hub-chatgpt-plugin`, its stdio command is the bare interpreter
name, its PATH is that interpreter's directory, and its arguments are
`-P -m pipecat_context_hub serve`. The CLI and server must resolve the same
configured index; do not persist unrelated environment variables or secrets.

Only use stdio where the selected host supports it. If host configuration is
unavailable from this execution environment, provide the prepared configuration
and exact remaining user steps, report MCP configuration pending, and retain
usable CLI retrieval. A cloud container's paths are not desktop interpreter
paths, and creating that container's CLI does not deploy an HTTPS MCP endpoint.
An existing remote MCP connection uses its server's runtime/index; do not install
local PCH unless CLI backup in the selected environment is requested.

## Cloud account and login

For Cloud/full-plugin onboarding, guide the user through account readiness;
retrieval-only or local build does not need Cloud login. After verifying the
Cloud CLI, inspect authentication through the supported read-only organisation
list. Capture output in memory and emit only allowlisted org IDs/names. Do not
print `auth whoami`, auth config, raw stdout/stderr or API-key listings: some
versions expose a Daily API key. Successful metadata reads establish existing
authentication; skip login for an already-authenticated user.

If an account is missing, direct the user to [Pipecat Cloud](https://pipecat.daily.co)
to create/sign in to their account. Explain personal workspaces and team
organisations using the
[account guide](https://docs.pipecat.ai/pipecat-cloud/fundamentals/accounts-and-organizations).
The user completes signup and any required organisation creation/joining in
the dashboard; do not collect passwords or automate account/billing changes.

For local interactive login, guide the user to run `pipecat cloud auth login`
in their own terminal on the execution host and complete the browser flow.
The [login reference](https://docs.pipecat.ai/api-reference/cli/cloud/auth#login)
explains opening the displayed URL manually when needed. Do not capture or
echo that one-time URL/token into model context. Login stores credentials in
that host's CLI configuration; it does not authenticate another cloud container.

For headless cloud/CI, follow the
[PAT guide](https://docs.pipecat.ai/pipecat-cloud/guides/personal-access-tokens):
the user creates a PAT in the dashboard and supplies `PIPECAT_TOKEN` through the
environment's secure secret mechanism. When supported, `pipecat cloud auth use-pat`
accepts a hidden prompt in the user's own interactive terminal. Never ask them
to paste tokens into chat, put values in argv or copy desktop auth files to a
different host. A public session API key is not proof of CLI management login.

After the user completes authentication, repeat the safe organisation-list check,
confirm the intended workspace/organisation and report Cloud account readiness
separately from CLI installation. Prefer explicit target flags during deployment;
do not switch a configured default organisation, log out, create keys or mutate
Cloud resources as part of readiness checking. Missing credentials or unavailable
metadata stay pending. Account/login setup does not approve a build or deployment.

## Verify and hand off

After a changed launch configuration, restart the actual MCP process through
the supported host flow before checking the packaged connection; a logical
reconnect may reuse the old process. Verify package-owned status and a read-only
`search_docs("TTS + STT")`, then status again. Check CLI `status` and the equivalent
search when native execution is available. Require hits for both concepts and
matching index/version provenance. These checks do not refresh the index.

Report installed/reused package versions, initial indexing performed or skipped,
configuration changes and backups, and separate PCH MCP/CLI, Pipecat build CLI,
Cloud CLI and Cloud account/organisation outcomes. PCH CLI success,
rendering or another MCP connection is not proof of packaged MCP activation.
Missing host restart/access remains pending; never call the whole setup complete
from PCH CLI-only proof. Full-plugin/Cloud setup also requires usable workflow
CLIs and successful account metadata checks; missing login remains pending.
Hand off to [Explore](../explore/SKILL.md) when the required
route is usable. Missing Cloud login does not block working Explore or local
Build. Build and Deploy retain their app-workspace, provider-credential and
concrete deployment approvals; setup does not access provider secrets, provision
Cloud deployments or start agent sessions.
