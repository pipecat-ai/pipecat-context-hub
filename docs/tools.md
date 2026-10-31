# MCP Tools

Part of the [Pipecat Context Hub documentation](README.md). For terminal usage, see the [CLI guide](cli.md).

| Tool | Use when... |
|------|-------------|
| `search_docs` | "How do I ...?" — conceptual questions, guides, configuration |
| `get_doc` | Retrieve a specific doc page by ID or path (e.g. `/guides/learn/transports`) |
| `search_examples` | "Show me an example of ..." — find working code by task or component |
| `get_example` | Retrieve full source files for a specific example |
| `search_api` | Class definitions, method signatures, frame types, inheritance |
| `get_code_snippet` | Get targeted code by symbol name, intent, or file path + line range |
| `check_deprecation` | Verify whether a pipecat import path is deprecated or removed (supports `at_version` for lifecycle at a specific version) |
| `get_hub_status` | Index health, reranker runtime state, record counts, framework version, commit SHAs, repo coverage (`indexed_repos`, `configured_no_records`, `catalog_groups_not_indexed`) |

All search results include an **EvidenceReport** with confidence scores,
source-grounded facts, unresolved questions, and suggested follow-up queries.

A search response may also carry a top-level **`corpus_hint`** when the query
names a [repo catalog](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/configuration.md#repo-catalog) group whose repos are not indexed: it
lists the missing repos and the `repos add` / `refresh` commands that would add
them. An alias matches whole words, including the words of a CamelCase or
snake_case identifier (`TavusVideoService`, `aws_bedrock`). The text checked is
`query` for `search_docs` and `search_examples`; `query`, `class_name` and
`module` for `search_api`; and `symbol`, `intent`, `class_name` and `module` for
`get_code_snippet`. The key is absent when no hint applies, and the hint does
not depend on result confidence. The one-shot CLI prints its generic stderr
poor-results hint only when there is no `corpus_hint`. `serve` reads configured and tainted repos
once at startup, so restart it after `repos add` / `repos remove`.

### Filters

`search_examples` supports filters to narrow results:

- `domain` — `"backend"` (Python), `"frontend"` (JS/TS), `"config"`, `"infra"`
- `language` — `"python"`, `"typescript"`
- `repo` — filter by GitHub repo slug
- `tags` — filter by capability tags

`search_api` supports filters for framework internals:

- `module` — module path prefix (e.g. `"pipecat.services"`)
- `class_name` — class name prefix (e.g. `"DailyTransport"`)
- `chunk_type` — `"method"`, `"function"`, `"class_overview"`, `"module_overview"`, `"type_definition"`
- `yields` — methods that yield a specific frame type
- `calls` — methods that call a specific method

### Multi-Concept Queries

Use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced
coverage.

## Version-Aware Queries

Pass `pipecat_version` to `search_examples`, `search_api` or `get_code_snippet` to get
results scored for compatibility with your project's pipecat version; results carry
`version_compatibility`. Details, filters and examples:
[Version-Aware Queries](https://github.com/pipecat-ai/pipecat-context-hub/blob/main/docs/version-aware-queries.md).
