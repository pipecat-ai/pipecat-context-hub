---
name: deploy
description: Prepare a Pipecat Cloud deployment with read-only checks and an exact approval payload, then execute only approved secret changes, build and deployment and verify readiness.
---

A deployment request authorises preparation in the selected app workspace.
Before concrete approval, Cloud inspection is read-only. Do not create/update
secrets, upload source, build images locally or remotely, push to a registry,
deploy, start agent sessions, delete deployments or roll back. `deploy` is not
a dry run. Skills do not grant execution permissions. Exploration and local
build remain available when Cloud prerequisites are missing. Pipecat CLI and
its Cloud extension are required for deployment, separately from PCH query
readiness. Offer [required CLI setup](../setup/SKILL.md#required-workflow-clis)
when they are absent; preparation does not install them silently.

## Discover and prepare

1. Confirm native local shell/file execution, selected existing app and installed
   Pipecat/Cloud CLI versions. Inspect installed help for deploy, secret list/set,
   organisations, regions, profiles, status and logs. Discover actual flags and
   config schema; installed capabilities take precedence over newer docs.
   Missing execution/CLI disables deployment only; never install an adapter or
   upgrade tools silently. Preserve the existing bot, dependency pins and lock;
   never re-scaffold an existing project to obtain Cloud files.
2. Follow [Explore's CLI retrieval](../explore/SKILL.md#cli-retrieval) using the
   installed PCH command. Check status before/after, retrieve relevant docs/API/
   examples and retain
   source citations. Separate multiple concepts with ` + ` or ` & `; inspect
   installed command help before passing filters. Ground Dockerfile
   and runtime entrypoint
   choices, including transport/runner compatibility, in retrieved definitions.
   Version annotations do not switch indexed snapshots. Missing/unknown evidence
   stays unresolved; never refresh/reset/repair or change the index/version pin.
3. Validate the selected path's supported runtime, entrypoint and deploy
   configuration. For source builds, also validate project layout, asynchronous
   bot entrypoint, dependency lock and Dockerfile. For source builds, verify
   required build tools against the exact pinned base, not a mutable tag or newer
   template.
   If absent, supply an explicit immutable tool input (for example, digest-pinned
   official uv binaries with the required platform and lock-format support).
   For source builds relying on the inherited entrypoint, inspect that base's
   config and revision-linked entrypoint source to establish its Python, PATH,
   workdir and dependency environment. Install into the Python
   environment the inherited entrypoint actually uses; default uv `.venv` creation
   is insufficient when that entrypoint runs system Python. Preserve base server
   dependencies (for example, an explicitly targeted `uv sync --inexact`, with
   conflicts and required imports checked) and inherited CMD. For source builds
   deliberately selecting a custom entrypoint, validate it against its own
   runtime and dependency environment. Preserve project/lock bytes for all source
   builds. Existing immutable registry images require verified image digest,
   platform and relevant runtime evidence, without a rebuild, dependency
   installation or unavailable base-source provenance.
   Manifests/source inspection proves inputs, not a successful build or runtime.
   Prepare only needed
   local files, preserving existing ones. Inspect required key **names** from
   placeholders/source without reading existing `.env` contents or printing
   values. Distinguish required provider keys from optional settings/defaults
   and Cloud management authentication. A local `.env` is not automatically
   transferred to Cloud. Do not copy it into an image or build context.
4. Check authentication through read-only organisation metadata. Never print
   `auth whoami`, local auth config or raw CLI output: some versions expose a
   Daily API key. Capture potentially sensitive output in memory, parse it and
   emit only allowlisted org IDs/names, region/resource metadata, secret-set
   names and key names. Withhold unsafe stderr, emails, credentials and values.
   If an account/login is missing, hand off to
   [Cloud account and login setup](../setup/SKILL.md#cloud-account-and-login):
   guide signup at `https://pipecat.daily.co`, then user-run
   `pipecat cloud auth login` on the execution host (or the supported PAT route
   for headless execution). Let the user complete authentication;
   successful read-only org queries already establish authentication. Do not
   log in/out or switch the configured organisation during preparation.
5. Resolve the user's organisation, region and agent identity explicitly; never
   choose the first/default org or infer deployment approval. Pass explicit
   organisation/region flags on Cloud commands where supported. Read existing
   agent and secret-set metadata, including same-name collisions across regions.
   Failed/unavailable metadata is unknown, never an empty existing state.
   Inspect only supported metadata calls; do not retrieve secret values. If key
   names cannot be inspected safely, record that prerequisite before proposing
   updates. Propose exact add/update/unchanged key names for the selected set;
   never overwrite unrelated keys or reuse an unrelated set automatically.
6. Validate proposed scaling/profile/session limits against help and read-only
   resources. Separate documented defaults, observed available profiles and
   unapproved proposals. Validate image/base-image architecture against the
   selected region; Daily-hosted arm64 is a dated observation, not a universal
   default. Read-only registry manifests can establish platform and digest
   without a build. Pin immutable base/build inputs where possible. Runtime
   compatibility and a successful image build remain unverified until exercised.
7. Choose a supported build path: Cloud source build, approved registry image or
   immutable GitHub revision. A Cloud `--build-dir` operation uploads source and
   builds/deploys, so it needs approval too; Docker is optional for that path.
   For local source-upload builds, before approval inspect the archive produced
   by the actual installed uploader's local preparation helper, without invoking
   upload/build/deploy or
   network operations. Docker `.dockerignore` negations cannot be assumed to
   work in the CLI uploader. Prefer a fresh strict staging directory containing
   only required regular, non-symlink source/build inputs, with verified
   uploader-compatible explicit exclusions. Preserve original app/config bytes;
   record any staging-only ignore-rule change separately. Exclude `.env*`,
   credentials, `.venv`, caches, reports and unrelated files without accessing
   secret content. Verify exact archive members and member-content hashes,
   required Dockerfile/lock/source presence, regular file types and absence of
   symlinks, traversal and extra paths. Record helper identity/version/provenance,
   context and archive hashes/size, and independent staging composition; use
   synthetic filenames/content to check exclusions. Local archive preparation
   is allowed; image build, upload or push still requires approval. If the actual
   archive cannot be verified safely, retain that prerequisite and stop before
   presenting the payload as approval-ready. For an approved registry image,
   verify its immutable image digest and platform; for an immutable GitHub
   revision, verify the revision and immutable build inputs. These paths do not
   require a local uploader archive.

## Concrete approval boundary

Prepare one reviewable payload with project/build context and immutable hashes,
verified uploader archive members/content hashes and archive hash/size for local
source uploads, or immutable image digest/platform or GitHub revision/build-input
evidence for the selected alternative path,
CLI/framework versions, org ID/name, region, agent name and create/update action,
architecture/base image digest, profile or explicit resources, scaling/session
limits, secret-set name and exact proposed key additions/updates. Include a safe
local secret-file path or supported value-transfer mechanism, execution order,
exact commands and readiness/log checks. Values stay outside model context,
command-line arguments, transcripts and committed files. Never claim credentials
are available based on placeholder names. Missing target, metadata, credential
availability or unsupported runtime is an explicit unresolved prerequisite.

Present this payload for approval only when required fields are concrete.
Preparation can complete with unresolved fields; execution cannot. A pending or
declined approval means zero mutations. User approval applies only to the stated
payload; changed files/hashes, target, resources or secret actions require a new
payload and approval. Recheck hashes and read-only target/secret metadata just
before execution; stop if the approved create/update semantics have changed.
Do not treat an earlier build request or general intent to deploy as approval.

## Execute approved operations and verify

After explicit approval of the concrete payload, transfer only approved secrets
using the installed supported local CLI/file mechanism. For CLI 1.3.0, discovered
`secrets set --file` supports a local file; do not pass values as positional
arguments or echo/cat the file. An existing file with unrelated keys needs a
local value-isolated transfer of only the approved keys. Do not rewrite secrets
when no changes are approved. Confirm redacted result; a failure stops the build.

Then execute the approved build/deploy in the selected workspace, using explicit
org/region/config and supported flags. Capture raw results/logs locally only
through a value-safe redaction/allowlist boundary; report identity, exit codes,
build IDs and states without secrets/PII. Wait with bounded status checks until
the matching deployment reports ready or a terminal failure/timeout occurs.
CLI exit zero or command issuance alone does not prove readiness. Inspect bounded,
redacted build/agent diagnostics on failure. Do not delete or automatically roll
back. An agent/provider session requires its own explicit approval unless it was
included in the approved payload; ready does not prove a voice conversation.

Return the immutable approved payload, deployed org/region/agent/version identity,
secret action names, build/deploy/readiness outcomes, diagnostics and limitations.
Record cleanup instructions for that identity without running deletion. Distinguish
source preparation, approved execution, ready deployment and session verification;
package rendering or candidate-source conversations do not prove installed skill
activation, ChatGPT Work activation or a live deployment. Never mark live Cloud
acceptance complete while approval or a required prerequisite remains missing.
