---
name: explore
description: Explore a voice-agent idea or Pipecat concept using the local Context Hub's documentation, API definitions and examples before making framework claims.
---

Use the plugin's Context Hub MCP tools for a read-only exploration through the
connection named `pipecat-context-hub-chatgpt-plugin`. A separately configured
Hub connection does not establish activation of this packaged connection.

1. Call `get_hub_status` first. Report index readiness, refresh date and indexed
   framework version. An empty, unavailable or stale index is a limitation;
   suggest remediation without running refresh or changing the index.
2. Retrieve relevant concepts with `search_docs`, definitions with `search_api`
   and examples with `search_examples`. Separate multiple concepts with ` + `
   or ` & `. Read needed detail using `get_doc`, `get_code_snippet` or
   `get_example` before making Pipecat-specific API assertions.
3. Cite returned source URLs or repository paths and distinguish retrieved
   evidence from inference. State missing evidence and version limitations.
   Inspect the available tool schemas before applying version or source
   preferences; never invent filters or treat a preference as index mutation.
4. Return a grounded explanation or approach and identify unresolved choices.
   An idea or concept question does not authorise scaffolding, file writes,
   secret uploads, builds or deployment. This feasibility package provides
   exploration only; build and deploy workflows are not implemented yet.

If the plugin's MCP connection is absent, report retrieval unavailable instead
of claiming that another connection or package validation proves activation.
