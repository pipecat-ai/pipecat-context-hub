# Local Pipecat Context Hub plugin

This optional desktop experiment packages explicit plugin/CLI setup,
grounded exploration, local app
build and approved Cloud deployment guidance with the installed Context Hub.
It does not bundle Python, the Context Hub runtime, Pipecat CLI or an index.
Local build requires native execution and an installed Pipecat CLI. Cloud
deployment additionally requires the Cloud CLI extension, account login and
approval of the exact payload.

Plugin installation copies the packaged skills, icons and MCP configuration;
it does not install dependencies, download models, create/refresh an index or
provision a Cloud service. PCH supplies both its CLI and MCP server. The
[Pipecat setup guide](https://docs.pipecat.ai/api-reference/context-hub#setup)
documents PCH bundled with `pipecat-ai[cli]`, exposing `pipecat context-hub`.
A standalone PCH installation exposes `pipecat-context-hub`. Older or separate
CLI environments may lack the bridge; verify `pipecat context-hub --help`
rather than assuming it exists. The local renderer binds the MCP
configuration to that existing installation. CLI backup or local stdio needs
PCH and an index in the task's execution environment; remote MCP uses its
server's runtime/index. Installing this plugin does not upload the local
runtime/index or install them in a cloud container.

## Set up Context Hub

After installing the plugin, ask **"Set up Pipecat Context Hub"**. The
[setup skill](skills/setup/SKILL.md) checks retrieval and the tools required for
the requested workflows. Full plugin setup includes the Pipecat CLI and Cloud
extension; explicitly retrieval-only setup needs only PCH. It reuses healthy
installations and the index. If prerequisites are missing, it presents
the required packages, initial-index and selected-client configuration changes, then
performs only authorised setup through native execution. One PCH installation
provides MCP and CLI; they are not separate downloads.

| Workflow | Required tools | Cloud account |
|---|---|---|
| Explore | PCH MCP by default, PCH query CLI as backup | Not needed |
| Build | Pipecat CLI and PCH retrieval | Not needed |
| Deploy | Pipecat CLI with Cloud extension and PCH retrieval | Authenticated login required |

The [fresh full-toolchain install](https://docs.pipecat.ai/api-reference/cli/overview#installation)
is `uv tool install "pipecat-ai[cli]" --with pipecatcloud`. Current CLI distributions
bundle PCH. Setup checks the actual installed commands and preserves existing
tool environments/extensions; the Cloud CLI is required for deployment,
independently of whether retrieval uses MCP or CLI.

For Cloud setup, the workflow guides new users to
[create/sign in to a Pipecat Cloud account](https://pipecat.daily.co), then run
`pipecat cloud auth login` on the execution host and complete browser login.
Already-authenticated users skip login. Headless cloud environments use the
documented PAT route through a secure `PIPECAT_TOKEN` mechanism. Account signup
and token entry are user actions, with no passwords/tokens pasted into chat.
The [login guide](skills/setup/SKILL.md#cloud-account-and-login) separates CLI
installation, account/organisation readiness and actual deployment approval.

Setup supports local computers and cloud execution environments with compatible
Python and subprocess access. It checks persistence and host configuration
capabilities rather than assuming a cloud container configures the desktop app.
If client configuration cannot be applied, it gives the remaining steps and
reports MCP pending separately from usable CLI. An existing remote MCP uses
its server's installation/index and needs no local PCH unless CLI backup is
requested. Setup does not provision a remote server or Pipecat Cloud services.

The workflow creates an initial index only when none exists; a healthy populated
index is reused. It preserves existing installations/configuration and makes
no automatic upgrade, refresh or repair. Existing corruption needs a separate
recovery request. Plugin installation itself still runs no dependency installer
or hook. Ordinary Explore requests remain read-only and can suggest Setup when
prerequisites are missing.

## Explore an idea or concept

Ask for an approach ("I want a browser voice assistant with TTS + STT") or a
specific concept ("Explain Pipeline frames" / "Show TTSService.run_tts").
Explore prefers the packaged MCP connection, with the installed PCH CLI as a
backup when that connection/tools are unavailable and native shell execution is
available. It checks the active route's readiness, retrieves relevant docs,
definitions and examples, and cites their sources. Proposed combinations are
labelled as inference; retrieved examples are not claimed to have been run.
Material choices can be clarified while independent concept retrieval proceeds.
Discussion requests remain read-only, including in hosts with execution tools.

State your target Pipecat version and source preference in the conversation.
The skill maps them to the installed tool schemas: example searches support
`repo`; docs support `area`, and API searches support module/class prefixes,
not a universal repository filter. Detail lookups retain the selected source's
provenance. Unsupported preferences are disclosed, not translated into invented
filters. Broad hits can reference a symbol without defining it; exploration
checks a detail or symbol lookup before making a definition claim.

`pipecat_version` annotates supported code/example searches; `compatible_only`
requires that target version and excludes known newer requirements, while
unknown compatibility can remain. These options do not switch the index or
validate APIs at an unindexed version. `check_deprecation` accepts `version`
for indexed-registry lifecycle evaluation, not snapshot selection. Missing,
stale or unavailable requested snapshots are explained without an automatic
refresh. Exploration requires working MCP or CLI retrieval. MCP needs no shell;
the CLI backup needs native execution and existing PCH, through either the
Pipecat CLI's verified `context-hub` subcommand or the standalone executable.
Standalone PCH needs no Pipecat CLI; neither route needs Cloud credentials.
Build and Deploy share Explore's retrieval fallback. CLI evidence is labelled
as such and does not prove MCP
activation; empty/low-confidence results or index failures do not trigger it.

## Build a local app

Explicitly request a build and select an empty project directory. The [build
skill](skills/build/SKILL.md) checks native shell access, installed CLI version,
help and JSON options, clarifies transport/providers/client/behaviour, validates
resolved dry-run output and scaffolds once. Existing apps are adapted without
re-scaffolding. It keeps generated constraints and records resolved dependency
versions, grounds customization in packaged Hub retrieval, and reports separate
import, startup and behaviour outcomes. Provider credentials are supplied locally
by the user; imports and credential-free checks do not verify a conversation.

CLI 1.3.0 was qualified with an explicit web/SmallWebRTC/cascade configuration,
Deepgram/OpenAI/Cartesia and no client. These are test inputs, not user defaults.
Local generation supports `--no-deploy-to-cloud`; no `--eval` flag is advertised.
See the repository evaluation report for exact flags, resolved versions and
limits. Build remains unavailable in discussion-only hosts or without CLI,
while exploration remains usable.

## Prepare a Cloud deployment

Request deployment preparation for the selected existing app. The [deploy
skill](skills/deploy/SKILL.md) discovers installed Cloud capabilities, verifies
account metadata, target, runtime/build files, region architecture and resources,
and records required key names and proposed secret-set changes. Existing apps
and locks are preserved. Auth output and secret values stay outside the chat;
failed metadata reads remain unresolved rather than meaning an empty account.

Preparation produces hashes, exact commands and one concrete payload covering
organisation/region/agent, build source, resources and secret changes. Explicit
approval precedes any secret write, local/Cloud build, source upload, registry
push or deployment. Approved secret changes precede build/deploy. Sessions and
deletion need explicit scope too. An approved command is verified through Cloud
readiness; readiness alone does not prove a provider conversation.

CLI 1.3.0's Cloud source-build path was exercised after exact payload approval.
On 2026-10-05, independent reads verified the qualification app's successful
build and matching READY deployment in the selected organisation and region,
with approved ARM64/resources/scaling/session limits and existing secret-set
identity. Earlier failed uploads/builds and their corrections are retained in
the repository evaluation report. The selected payload required zero secret
writes; readiness and historical startup logs do not verify provider credentials
or a voice session. Each new app still needs its own target, credential readiness
and concrete approval. Candidate-source qualification and fresh rendering do
not establish revised installed-skill activation or ChatGPT Work activation.

## Qualification limits

The recorded Codex local experiment demonstrates packaged exploration, CLI
1.3.0 scaffolding, credential-free local imports/construction/startup and the
approved Cloud READY result. It uses indexed Pipecat 1.12.0; the latest recorded
Hub status is 0.8.1 with an October 5 refresh. These are dated observations,
not guarantees about other hosts, versions, transports or provider combinations.
JSON scaffold config execution, existing-app adaptation, absent-execution hosts,
provider/browser sessions, secret writes and ChatGPT Work remain untested.
The repository [evaluation report](https://github.com/pipecat-ai/pipecat-context-hub/blob/feature/chatgpt-local-plugin/docs/evaluations/pipecat-context-hub-plugin.md)
distinguishes manual/synthetic walkthroughs, source-candidate conversations,
actual packaged activation and live acceptance. Re-rendering propagates current
instructions; activation of revised cached skills needs its own host test.

## Cat-mark branding

The package includes square 512 × 512 SVGs in black for light backgrounds and
white for dark backgrounds. The larger `logo` / `logoDark` assets add a small
`context-hub` label below the cat, aligned with the outer right whisker edge.
The label is outlined vector geometry, so rendering needs no installed font.
The `composerIcon` / `composerIconDark` assets retain the standalone cat for
small sizes. The renderer copies these four named assets; unrelated files in
`assets/` are excluded.

The mark matches the [provided Pipecat brand folder](https://drive.google.com/drive/folders/10PXwYdU15L_7hgJqoAkuMmsxg_jNoeJY)
(`Mark + Black Text [Traditional].png`). Its five vector paths are isolated
from the supplied `daily + pipecat horizontal lockup - black.svg`, preserving
the cat geometry while omitting the original wordmarks. The white variants
change only the fill. All assets retain transparent backgrounds. Pipecat's mark remains
Pipecat branding.

Re-render the marketplace source and reinstall the local plugin to propagate
the assets, then reopen the plugin page to verify the host displays the mark.

## Prepare a local copy

Install Context Hub separately and have a populated local index. Find its
absolute Python command with `pipecat-context-hub install --print-config`
(this prints configuration without registering a client). Run that Python:

```sh
"/absolute/installed/hub/python" -P plugins/pipecat-context-hub/scripts/prepare_local.py \
  /absolute/local-marketplace/plugins/pipecat-context-hub
```

The destination must be outside this checkout and absent or empty. The
renderer checks importability, copies only named package files and skill
entrypoints, preserves `mcp.template.json`, and creates root `mcp.json` with
the installed interpreter’s bare executable name, `PATH` containing only its
absolute interpreter directory, and `-P -m pipecat_context_hub serve`.
Interpreter symlinks retain virtual-environment identity. The portable loader
rejects an absolute `command`; the pinned `PATH` provides no ambient/system
Python fallback. Extra launch fields or environment values are refused. The packaged
MCP server is named `pipecat-context-hub-chatgpt-plugin`, distinct from the
standalone `pipecat-context-hub` registration. The renderer rejects templates
containing any other server entries. It does not
register the package or refresh the index. Never register the unresolved
template. No machine-specific launch paths belong in this source package.

## Install and verify separately

Follow [OpenAI's local marketplace instructions](https://developers.openai.com/plugins/build/plugins).
In the local marketplace root create `.agents/plugins/marketplace.json`:

```json
{
  "name": "pipecat-hub-local",
  "interface": {"displayName": "Local Pipecat Hub"},
  "plugins": [{
    "name": "pipecat-context-hub",
    "source": {"source": "local", "path": "./plugins/pipecat-context-hub"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

Use `codex plugin marketplace add /absolute/local-marketplace` to add that
source; inspect installed `codex plugin --help` for supported commands.
Install from the desktop Plugins Directory (or `codex plugin add
pipecat-context-hub@pipecat-hub-local` if supported), then open a new local
chat after refreshing/restarting the host as required. Confirm the plugin
and `explore` skill are discovered, invoke exploration, and observe its own
MCP initialize and `get_hub_status` calls through
`pipecat-context-hub-chatgpt-plugin`. A subprocess smoke or an existing
Hub registration cannot prove this plugin activated in the desktop.

Record retrieval, execution and Cloud prerequisites independently in
`docs/evaluations/pipecat-context-hub-plugin.md` in the
[source repository](https://github.com/pipecat-ai/pipecat-context-hub).
Evaluation reports stay in repository documentation and are not copied into
the installed plugin. Local stdio support is host-dependent.
Discussion-only hosts cannot promise local file creation. Exploration needs
neither Pipecat CLI nor Cloud credentials; build additionally
needs local shell and Pipecat CLI, and Cloud deployment requires the Cloud
extension and authenticated account. Discover versions and flags from installed help.

After changing the plugin or installed interpreter, render a fresh destination
and use the host's documented marketplace refresh/reinstall flow. Disable or
remove this plugin using the host's plugin controls; remove its marketplace
only when no other plugins need it. Keep the Hub installation and index for
other clients. Never remove unrelated registrations.
