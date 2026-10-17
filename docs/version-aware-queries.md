# Version-Aware Queries

Part of the [Pipecat Context Hub README](README.md).

If your project targets a specific pipecat version, pass `pipecat_version` to
get results scored for compatibility:

```
search_examples("TTS pipeline", pipecat_version="0.0.96", domain="backend")
search_api("DailyTransport", pipecat_version="0.0.96")
```

Results are annotated with `version_compatibility`: `"compatible"`,
`"newer_required"`, `"older_targeted"`, or `"unknown"`. Use
`version_filter="compatible_only"` to exclude results requiring a newer version.

The framework repo is indexed at its newest release tag (`latest`), which
re-resolves on every refresh — a plain incremental `refresh` picks up a new
release without `--force`. Prereleases are skipped unless the repo has nothing
else.

After a pipecat upgrade, though, prefer `refresh --force`. A new release replaces
the framework repo's records, and Chroma only marks deleted vectors dead: the
persisted graph keeps them and never reuses their slots, so each plain refresh
that replaces records leaves the graph larger than the live corpus. Only a
`--force` refresh compacts it (it re-ingests every source, so it is slower).
Skipping it is safe: queries stay correct and only the graph file is oversized.

That default keeps the framework aligned with the rest of the index: the docs
site publishes from a release-time promotion, and the example repos depend on
released wheels. A default-branch framework checkout would be the only source
contributing unreleased APIs, and since `indexed_framework_version` is a
git-describe *floor*, those APIs would be stamped with the previous release's
number — which reads as "compatible" to a caller running that release.

Pin a different version, or ask for the default branch, with
`--framework-version`:

```bash
uvx pipecat-ai-context-hub refresh --framework-version v0.0.96
uvx pipecat-ai-context-hub refresh --framework-version head    # or `main`
# or via env var:
PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96 uvx pipecat-ai-context-hub refresh
```

Use `head` when you are working against unreleased framework code — developing
Pipecat itself, or building on a feature that has not shipped yet.

A tag that does not exist fails the refresh immediately with exit 1, listing
the newest tags that do — the pin is resolved before any indexing work, so a
typo costs a second rather than a full crawl. A *transient* clone failure is
treated differently: it is logged as a warning and the remaining repos are
still indexed, so one flaky source cannot fail the whole refresh.

Note that a pin applies only to the framework repo (`pipecat-ai/pipecat`).
The examples, flows, and client SDK repos always track their default branch,
and docs come from the live `docs.pipecat.ai`, which is unversioned.
