# Pipecat Context Hub plugin changelog

Release notes for the skills-only Pipecat Context Hub plugin. The repository
root `CHANGELOG.md` covers the Context Hub package; this file covers the
independent plugin release history and is included in prepared plugin copies.

## [Unreleased]

### Changed

- Candidate 0.2.4 improves voice and multimodal discovery wording, batches
  read-only setup checks into one approval proposal, and offers an explicit,
  approval-gated upgrade path for an older Pipecat CLI.
- The candidate keeps host-controlled plugin surfacing and approval settings
  explicit; it does not install itself before activation or upgrade a host
  silently.

## [0.2.3] - Published

### Added

- Skills-only Setup, Explore, Build and Deploy workflows using Pipecat Context
  Hub CLI queries for grounded documentation, API definitions and examples.
- Approved publisher metadata, product and support links, Daily's public
  privacy-policy and terms-of-service links, three starter prompts, and a
  bundled privacy notice.

### Changed

- Listing and Explore metadata describe voice AI, STT/LLM/TTS and real-time
  speech-to-speech integrations without claiming automatic recommendation or
  selection in ChatGPT.
