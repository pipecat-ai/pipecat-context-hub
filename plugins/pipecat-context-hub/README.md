# Pipecat Context Hub skills-only plugin

Explore voice AI ideas, choose language and speech models supported by Pipecat,
and build agents using sourced documentation, API definitions and examples. Explore
STT/LLM/TTS pipelines and real-time speech-to-speech through
[Pipecat's supported services](https://docs.pipecat.ai/api-reference/server/services/supported-services).
The listing describes these capabilities in general terms. Explore retrieves
documentation and examples for the specific services relevant to the user's idea;
automatic recommendation/selection in ChatGPT remains untested.

This plugin packages four workflows: Setup, Explore, Build and Deploy. It uses
PCH command-line queries for grounded documentation, API definitions and examples.
It contains no MCP configuration or lifecycle hooks. The underlying Context Hub
package continues to support MCP for other clients.

Plugin installation copies skills and cat-mark assets; it does not install Python,
PCH, Pipecat CLI, an index or Cloud credentials. Each workflow needs native command
execution in the task's local or cloud environment. Installing on the web does not
provision those capabilities. Environments without execution can show guidance but
cannot run grounded queries or builds through this plugin.

## Set up Context Hub

After installing, ask **"Set up Pipecat Context Hub"**. The
[setup skill](skills/setup/SKILL.md) detects native execution, installed commands,
index readiness and the tools required for the requested workflows. It reuses
healthy installations and indexes. When prerequisites are missing, it presents
the required packages, initial-index path/sources and storage requirements, then
performs only authorised setup. No MCP registration or binding is required.

| Workflow | Required tools | Cloud account |
|---|---|---|
| Explore | Native execution and PCH query CLI | Not needed |
| Build | Pipecat CLI and PCH retrieval | Not needed |
| Deploy | Pipecat CLI with Cloud extension and PCH retrieval | Authenticated login required |

The [fresh full-toolchain install](https://docs.pipecat.ai/api-reference/cli/overview#installation)
is `uv tool install "pipecat-ai[cli]" --with pipecatcloud`. Current CLI distributions
bundle PCH. Verify `pipecat context-hub --help`; older installations may need the
standalone `pipecat-context-hub` command supplied by `pipecat-ai-context-hub`.
Setup retains existing working versions/extensions instead of silently replacing
them. An explicitly retrieval-only setup can use standalone PCH without Pipecat
CLI or a Cloud account.

For Cloud setup, guide users to [create/sign in to a Pipecat Cloud account](https://pipecat.daily.co),
then run `pipecat cloud auth login` in their own terminal on the execution host
and complete browser login. Already-authenticated users skip login. Headless
execution uses the documented PAT route through a secure `PIPECAT_TOKEN`
mechanism. Passwords and tokens stay out of chat. The
[account/login guide](skills/setup/SKILL.md#cloud-account-and-login) separates
CLI installation, authenticated organisation readiness and deployment approval.

Setup checks Python/subprocess access, download permissions and runtime/index
persistence on the actual host. Local installation/login does not authenticate
another cloud container. Initial indexing can download models/source repositories;
it needs explicit setup authorisation and an absent or demonstrably empty index.
A stale populated index is reused; repair/reset and upgrades are separate requests.
Fresh-user installation and cloud persistence remain unqualified until observed.

## Explore an idea or concept

Use the [explore skill](skills/explore/SKILL.md) for a voice-agent idea or framework
concept. It discovers the PCH query commands, checks `status`, retrieves relevant
concepts and detail, and cites returned source URLs. It uses `pipecat context-hub`
when available, otherwise standalone `pipecat-context-hub`. Both dispatch the same
handlers and return JSON on stdout; diagnostics remain on stderr.

For example, after verifying the installed command:

```sh
pipecat context-hub status
pipecat context-hub search-docs "TTS + STT"
pipecat context-hub search-examples "TTS + STT" --domain backend
```

Multi-concept searches use ` + ` or ` & ` for balanced coverage. Fetch the relevant
page, example or symbol detail before making framework claims. Installed help
controls flags, including source/version preferences. Compatibility annotations
are not a different source snapshot or proof of compatibility when unknown.
A `latest` pin is not proof of today's latest release.

Exploration is read-only: no installs, indexing, file creation, example execution,
secret access or deployment. Missing runtime/index offers explicit Setup. CLI
status before/after confirms unchanged index/version provenance; no refresh occurs.
A stale index can provide dated evidence with a stated coverage limit. Neither PCH
query route needs a Cloud account.

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

The earlier stdio Codex local experiment demonstrated packaged exploration, CLI
1.3.0 scaffolding, credential-free local imports/construction/startup and the
approved Cloud READY result. It uses indexed Pipecat 1.12.0; the latest recorded
Hub status is 0.8.1 with an October 5 refresh. These are dated observations,
not qualification of this CLI-first release, other hosts or provider combinations.
JSON scaffold config execution, existing-app adaptation, absent-execution hosts,
provider/browser sessions, secret writes and ChatGPT Work remain untested.
The repository [evaluation report](https://github.com/pipecat-ai/pipecat-context-hub/blob/feature/chatgpt-local-plugin/docs/evaluations/pipecat-context-hub-plugin.md)
distinguishes manual/synthetic walkthroughs, source-candidate conversations,
actual packaged activation and live acceptance. Preparing a fresh copy propagates current
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

Prepare a fresh marketplace source and reinstall the local plugin to propagate
the assets, then reopen the plugin page to verify the host displays the mark.

## Prepare a portable copy

Package preparation requires only Python's standard library, not an installed Hub
or index. From this checkout, run a compatible Python:

```sh
python3 -I -S plugins/pipecat-context-hub/scripts/prepare_local.py \
  /absolute/local-marketplace/plugins/pipecat-context-hub
```

The destination must be outside this checkout and absent or empty. The helper
copies exactly thirteen resources: manifest, README, privacy notice, BSD licence,
itself, four skill entrypoints
and four SVGs. It rejects missing/symlinked required resources and unsafe
or nonempty destinations. It performs no runtime installation or indexing, and
copies no MCP configuration, hooks, private reports or evaluation artifacts.
The package is independent of the packaging machine's interpreter/index paths.

## Install and verify

Place the prepared copy in the selected local marketplace. A marketplace root
can contain `.agents/plugins/marketplace.json` with:

```json
{
  "name": "pipecat-hub-local",
  "interface": {"displayName": "Local Pipecat Hub"},
  "plugins": [{
    "name": "pipecat-context-hub",
    "source": {"source": "local", "path": "./plugins/pipecat-context-hub"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools"
  }]
}
```

Use `codex plugin marketplace add /absolute/local-marketplace` and install
`pipecat-context-hub@pipecat-hub-local`, using the installed CLI's supported flags.
After updates, prepare a fresh copy, preserve the old source as a backup and
reinstall through the supported host flow. Verify version 0.2.3, four skills,
cat-mark resources and absence of bundled MCP servers in the installed package.
Standalone Hub/client registrations are unrelated and remain unchanged.

Start a new supported chat and invoke Setup or Explore. Observe skill loading and
native PCH queries with sourced output. Successful packaging, direct CLI commands
and cache parity do not prove autonomous installed-skill activation. Record
runtime/index, Build and Cloud prerequisites independently in your verification notes.
No evaluation report is included in the installed package.

## Publication

This is a skills-only release candidate for supported execution environments.
A public submission ZIP excludes MCP configuration and hooks, so it needs no
hosted PCH HTTPS endpoint. Version 0.2.3 includes the approved corporate publisher
label, official product/support links, Daily's public privacy-policy and
terms-of-service URLs and three starter prompts. The dashboard uses the selected
verified Developer identity for
the public publisher name. The bundled [privacy notice](PRIVACY.md) explains local
index/query processing, information returned to chat, downloads and Cloud uploads,
and links to Daily's policy and Trust Center. It is included in the submission ZIP;
publishing a separate documentation page is not performed by packaging.
Clean-environment tests and portal skill scans/review remain separate qualifications.
Package installation does not provision command execution or persistent storage.
See [submission requirements](https://developers.openai.com/plugins/deploy/submission).
OpenAI currently does not support adding MCP to the same skills-only listing later;
a future MCP offering needs its own supported distribution decision.
