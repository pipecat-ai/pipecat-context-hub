# Phase 2: Tree-sitter TypeScript Extraction

**Status:** Not Started
**Priority:** High
**Branch:** `feature/tree-sitter-ts-phase2`
**Created:** 2026-03-30
**Objective:** Replace the Phase 1a regex parser with tree-sitter-based AST
extraction. Key improvement: individual method chunks from class bodies, full
parameter types, return types, and method signatures — the major gap in Phase 1.

## Context

Phase 1a (v0.0.12) added regex-based TypeScript source parsing that extracts
exported interfaces, classes, type aliases, functions, enums, and typed const
exports. It produces ~1,450 TS source chunks across 6 SDK repos. However:

- **No method extraction** — class bodies are indexed as a single
  `class_overview` chunk. Users searching for `PipecatClient.connect()` or
  `Transport.initialize()` can't find individual method signatures.
- **No parameter types** — `method_signature` is always empty for TS chunks.
- **Regex fragility** — complex patterns like generic return types
  (`Promise<{ url: string }>`) required multiple rounds of fixes. Tree-sitter
  handles these correctly by construction.
- **No call/import analysis** — `calls`, `yields`, `imports` metadata are
  empty for all TS chunks.

Tree-sitter provides a proper AST that resolves all of these.

## Requirements

1. **Drop-in replacement** — `source_ingest.py` continues to call the same
   entry point (`parse_ts_source`). The function returns the same
   `TsDeclaration` type (or an expanded version) so `_build_ts_chunks` works
   with minimal changes.
2. **Backward compatible** — all 22 existing MCP smoke tests must continue
   to pass. Chunk IDs may change (acceptable for a minor version bump).
3. **Method chunks** — classes and interfaces emit individual `method` chunks
   with `method_name`, `method_signature`, `class_name`, and `base_classes`.
4. **Same or better coverage** — every declaration the regex parser finds
   must also be found by tree-sitter. Net chunk count should increase (methods).
5. **New dependencies** — `tree-sitter>=0.25,<1.0` and
   `tree-sitter-typescript>=0.23,<1.0` (both available on PyPI).
6. **Offline-safe** — tree-sitter grammars are bundled in the pip package,
   no network fetch at parse time.
7. **Performance** — parsing should be comparable or faster than regex
   (tree-sitter is C-based, typically faster for large files).

## Implementation Checklist

### Phase 2a: Add dependencies and scaffold

- [ ] Add `tree-sitter` and `tree-sitter-typescript` to `pyproject.toml`
- [ ] Run `uv lock` and verify install
- [ ] Create `ts_tree_sitter_parser.py` with a minimal `parse_ts_source()`
      that uses tree-sitter to parse and return empty `TsDeclaration` list
- [ ] Verify tree-sitter loads the TypeScript grammar correctly
- [ ] Write a smoke test that parses a real `.ts` file from the cloned repos

### Phase 2b: Extract top-level declarations (parity with regex)

- [ ] Export detection — walk `export_statement` nodes
- [ ] Interface extraction — `interface_declaration` with extends, generics
- [ ] Class extraction — `class_declaration` with extends, implements,
      abstract modifier
- [ ] Type alias extraction — `type_alias_declaration`
- [ ] Function extraction — `function_declaration` with params and return type
- [ ] Enum extraction — `enum_declaration`
- [ ] Const export extraction — `lexical_declaration` with type annotation
      inside `export_statement`
- [ ] JSDoc extraction — `comment` nodes immediately before declarations
- [ ] All Phase 1a unit tests pass against the new parser
- [ ] A/B comparison: run both parsers on all 6 TS repos, verify same
      declarations found (allow tree-sitter to find more, not fewer)

### Phase 2c: Method extraction (new capability)

- [ ] Class method extraction — `method_definition` nodes within
      `class_body`, including:
      - Method name, parameter types, return type
      - Access modifiers (public/private/protected)
      - Abstract vs concrete
      - Static methods
      - Getters and setters
      - Constructor
- [ ] Interface method extraction — `method_signature` and
      `property_signature` nodes within `interface_body`
- [ ] Build `method_signature` string (e.g., `(url: string, opts?: Options) => Promise<void>`)
- [ ] Wire into `_build_ts_chunks` — emit `chunk_type="method"` records
      with `class_name` and `method_name` populated
- [ ] Add `_MIN_METHOD_LINES` filtering for TS (matching Python convention)
- [ ] Unit tests for method extraction: concrete, abstract, static,
      getter/setter, constructor, overloaded

### Phase 2d: Enhanced metadata

- [ ] Populate `method_signature` field for function and method chunks
- [ ] Populate `imports` — extract `import { X } from "..."` statements
- [ ] Populate `calls` — extract `this.method()` calls from method bodies
      (best-effort, like Python's `_extract_calls`)
- [ ] Parameter extraction with types and defaults for all functions/methods
- [ ] Decorator extraction (`@override`, `@deprecated`, etc.)
- [ ] Update smoke tests for method-level queries:
      - `search_api("connect", class_name="PipecatClient")` → method chunk
      - `search_api("initialize", class_name="Transport")` → abstract method

### Phase 2e: Cleanup and validation

- [ ] Remove `ts_source_parser.py` (regex parser) — fully replaced
- [ ] Update `source_ingest.py` imports
- [ ] Full test suite passes (735+ tests)
- [ ] `ruff check` and `mypy` clean
- [ ] All 22 MCP smoke tests pass
- [ ] Live `refresh --force` — verify chunk counts (should increase due
      to method chunks)
- [ ] Performance comparison: time `parse_ts_source` on
      `pipecat-client-web` repo with both parsers

## Technical Specifications

### Files to Create

| File | Purpose |
|------|---------|
| `src/.../ingest/ts_tree_sitter_parser.py` | Tree-sitter extraction (replaces regex parser) |
| `tests/unit/test_ts_tree_sitter_parser.py` | Tests for the new parser |

### Files to Modify

| File | Changes |
|------|---------|
| `pyproject.toml` | Add tree-sitter deps |
| `source_ingest.py` | Import new parser, emit method chunks |
| `test_ts_source_parser.py` | Re-point to new parser, add method tests |

### Files to Delete

| File | Reason |
|------|--------|
| `ts_source_parser.py` | Replaced by tree-sitter parser |

### Architecture Decision: Parser Interface

The new parser should return the same `TsDeclaration` dataclass but with
an expanded `kind` set to include `"method"`. This minimizes changes to
`_build_ts_chunks`.

```python
# Expanded kind values:
# Phase 1: "interface", "class", "type_alias", "function", "enum", "const"
# Phase 2 adds: "method", "constructor", "getter", "setter"
```

The `_TS_KIND_TO_CHUNK_TYPE` mapping in `source_ingest.py` expands:
```python
_TS_KIND_TO_CHUNK_TYPE = {
    # ... existing ...
    "method": "method",
    "constructor": "method",
    "getter": "method",
    "setter": "method",
}
```

For method declarations, `class_name` comes from the enclosing class, and
`method_signature` is populated with the full typed signature.

### Tree-sitter API Usage

```python
from tree_sitter import Parser
from tree_sitter_typescript import language_typescript

parser = Parser(language_typescript())
tree = parser.parse(source_bytes)
root = tree.root_node
```

Key node types to handle:
- `export_statement` → check child for declaration type
- `class_declaration` → name, type_parameters, extends_clause, implements_clause, class_body
- `interface_declaration` → name, type_parameters, extends_type_clause, object_type
- `type_alias_declaration` → name, type_parameters, value
- `function_declaration` → name, type_parameters, formal_parameters, return_type, statement_block
- `enum_declaration` → name, enum_body
- `lexical_declaration` → for const exports with type annotation
- `method_definition` → within class_body
- `abstract_method_signature` → within class_body
- `method_signature` → within interface object_type
- `comment` → JSDoc blocks

### Integration Seams

| Seam | Contract |
|------|----------|
| `parse_ts_source(source: str) -> list[TsDeclaration]` | Same signature, same return type. Tree-sitter replaces regex internally. |
| `_build_ts_chunks(declarations=..., ...)` | Handles new `kind="method"` values. Emits `chunk_type="method"` records. |
| `_TS_KIND_TO_CHUNK_TYPE` | Expanded with method/constructor/getter/setter mappings. |
| `_render_ts_snippet(decl, module_path)` | Handles new kind labels for method/constructor/getter/setter. |
| Existing smoke tests 14-22 | Must still pass — class_overview chunks still emitted for classes/interfaces. |

## Review Focus

- **Backward compatibility** — Phase 1a regex output must be a subset of
  Phase 2 tree-sitter output. No declarations should be lost.
- **Node type coverage** — tree-sitter-typescript grammar has many node types.
  Verify we handle the ones that appear in the 6 target repos.
- **Performance** — tree-sitter parser initialization (loading grammar) should
  be lazy/cached, not per-file.
- **Error recovery** — tree-sitter handles malformed files gracefully (partial
  parse). Verify we don't crash on syntax errors.
- **Method chunk quality** — method snippets should be useful for search, not
  just raw source dumps. Include JSDoc, signature, and body.

## Testing Notes

### A/B Comparison Strategy

Before removing the regex parser, run both parsers on all 6 TS repos and
compare output:

```python
for repo in TS_REPOS:
    regex_decls = regex_parse(source)
    treesitter_decls = treesitter_parse(source)
    # Every regex declaration must appear in treesitter output
    # Treesitter may find additional declarations (methods)
    assert set(regex_names) <= set(treesitter_names)
```

### New Smoke Tests to Add

- `search_api("connect", class_name="PipecatClient")` → method chunk from
  pipecat-client-web
- `search_api("initialize", class_name="Transport")` → abstract method
  from pipecat-client-web

## Acceptance Criteria

- [ ] All 22 existing MCP smoke tests pass
- [ ] New method-level smoke tests pass
- [ ] `ts_source_parser.py` (regex) fully removed
- [ ] Net chunk count increases (method chunks added)
- [ ] `method_signature` populated for all TS function/method chunks
- [ ] 735+ unit tests pass, lint and type check clean
- [ ] Performance: tree-sitter parse time <= regex parse time on
      pipecat-client-web (859+ code chunks repo)
