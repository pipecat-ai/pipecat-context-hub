# Pipecat Context Hub plugin changelog

Release notes for the skills-only Pipecat Context Hub plugin. The repository
root `CHANGELOG.md` covers the Context Hub package; this file covers the
independent plugin release history and is included in prepared plugin copies.

## [Unreleased]

### Changed

- Candidate 0.2.4 improves voice and multimodal discovery wording, batches
  read-only setup checks into one approval proposal, and offers an explicit,
  approval-gated upgrade path for an older Pipecat CLI.
- Candidate 0.2.4 now explains the current CLI/MCP corpus-coverage signals:
  `corpus_hint`, catalog groups missing from the index, refresh-in-progress
  exit 75, and the privacy boundary around locally configured repository names.
- The candidate keeps host-controlled plugin surfacing and approval settings
  explicit; it does not install itself before activation or upgrade a host
  silently.

## [0.2.3] - Published

> First publicly available plugin release. This release consolidates all work
> from the earlier internal and pre-publication revisions.

### Added

- Skills-only Setup, Explore, Build and Deploy workflows using Pipecat Context
  Hub CLI queries for grounded documentation, API definitions and examples.
- Native `pipecat context-hub` retrieval is preferred when available, with the
  standalone `pipecat-context-hub` command as a fallback. Build and Deploy
  workflows retain their local execution, Pipecat CLI and Cloud-login
  requirements.
- Approved publisher metadata, product and support links, Daily's public
  privacy-policy and terms-of-service links, three starter prompts, and a
  bundled privacy notice with the packaged branding assets.

### Changed

- Listing and Explore metadata describe voice AI, STT/LLM/TTS and real-time
  speech-to-speech integrations without claiming automatic recommendation or
  selection in ChatGPT.
- The portable package contains skills and assets, not Python, the Hub runtime,
  an index, credentials or Cloud infrastructure. Setup discovers the host and
  proposes only explicitly approved local changes.

### Removed

- MCP configuration and lifecycle hooks were removed from the public plugin
  after the prototype because the supported local HTTP/certificate path was
  unavailable. Standalone Context Hub MCP access remains supported for other
  clients.
