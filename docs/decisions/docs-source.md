# Decision: Documentation Source — llms-full.txt over HTML Crawling and DeepWiki

**Date:** 2026-02-18
**Status:** Accepted

## Context

We needed a reliable way to ingest Pipecat documentation into the local index.

Three options were considered:

1. **HTML crawling** of `https://docs.pipecat.ai/` — BFS crawl with BeautifulSoup
2. **DeepWiki** (`https://deepwiki.com/pipecat-ai/pipecat/`) — third-party mirror
3. **llms-full.txt** (`https://docs.pipecat.ai/llms-full.txt`) — official pre-rendered file

## Decision

Use `llms-full.txt` as the sole documentation source. Drop both HTML crawling and DeepWiki.

## Rationale

**HTML crawling failed.** The docs site (`docs.pipecat.ai`) is a Mintlify SPA. `httpx.get()` returns
JavaScript shells with no rendered content, producing 0 docs records. A headless browser would fix
this but adds heavy dependencies (Playwright/Selenium) for no benefit when a better source exists.

**DeepWiki is redundant.** It's a third-party mirror that may lag behind official docs. Since
`llms-full.txt` is hosted on the same domain as the official docs and contains the complete content,
there's no recall gap to fill.

**llms-full.txt is ideal:**
- Official source, hosted at `docs.pipecat.ai/llms-full.txt`
- Pre-rendered markdown — no JS rendering, no HTML parsing needed
- Complete: 305 pages, 1.7 MB, all documentation content
- LLM-friendly format with clear page boundaries (`# Title` + `Source: URL`)
- Single HTTP GET replaces an entire crawl pipeline
- Always current (updated with the docs site, last-modified tracked by server)

## Consequences

- Removed `beautifulsoup4` and `markdownify` dependencies
- Removed `deepwiki_enabled` and `deepwiki_urls` config fields
- DocsCrawler now fetches one file instead of crawling hundreds of pages
- Mintlify XML-like tags (`<Note>`, `<ParamField>`, etc.) cleaned to markdown
- If Pipecat ever removes `llms-full.txt`, we'd need to revisit (unlikely — it's
  part of the Mintlify platform standard for LLM consumption)
