#!/usr/bin/env python3
"""Live-hub smoke test for ``check_deprecation`` false/true positives.

Runs the ``check-deprecation`` CLI subcommand against the **built local index**
for a curated set of canary symbols (AGENTS.md items #34–37, #46–48) and asserts
the ``deprecated`` verdict. Unlike the offline fixture smoke under
``tests/smoke/``, this hits the real persisted ``deprecation_map.json``, built
from pipecat's deprecation registry (PR #85), and therefore requires a prior
``refresh`` — it is intentionally NOT part of the pytest gate.

Two directions are checked:

* CURRENT — current, non-deprecated APIs that must return ``deprecated: false``.
  A false positive here (current API flagged deprecated) is the worst failure
  mode the tool has. Includes ancestor packages and owner-of-member classes,
  which forward-prefix matching must NOT flag.
* DEPRECATED — genuinely deprecated APIs that must stay ``deprecated: true``.
  Guards against the lookup over-narrowing and silently dropping real
  deprecations.

Note: the registry only carries symbols that still exist in the indexed pipecat
source with an active marker. Already-*removed* symbols are absent and correctly
report ``deprecated: false``, so they are not valid DEPRECATED canaries. The
symbols below track the indexed pipecat version; re-verify if they drift.

Usage::

    uv run python scripts/smoke_check_deprecation.py          # full run

Exit codes: 0 all canaries pass, 1 one or more regressions.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 — hardcoded CLI args, no user input
import sys

# Current APIs — must report deprecated: false.
CURRENT: dict[str, str] = {
    # Stable core classes — version-independent regression canaries.
    "Pipeline": "core class",
    "CartesiaTTSService": "core class",
    "SileroVADAnalyzer": "core class",
    "DailyTransport": "core class",
    # Ancestor packages of a deprecated descendant module (forward-prefix only).
    "pipecat.services": "ancestor of deprecated pipecat.services.grok.llm",
    "pipecat.services.openai.llm": "current module owning deprecated params",
    # Owner-of-member: class owns a deprecated member/param but is itself current.
    "GladiaSTTService": "owner of deprecated nested member",
    "OpenAILLMService": "owner of deprecated parameter(s)",
}

# Genuinely deprecated APIs (still present in source with a marker) — must report
# deprecated: true.
DEPRECATED: dict[str, str] = {
    "pipecat.services.grok.llm": "module move -> pipecat.services.xai.llm",
    "ResampyResampler": "class -> SOXRAudioResampler",
    "PipelineTask": "class -> PipelineWorker (1.3.0)",
    "PipelineRunner": "class -> WorkerRunner (1.3.0)",
    "InterruptionTaskFrame": "class -> InterruptionWorkerFrame",
}


def _check(symbol: str) -> dict[str, object]:
    """Run the check-deprecation CLI and return the parsed JSON verdict."""
    result = subprocess.run(  # nosec B603 B607 — hardcoded args, symbol is from our own dicts
        ["uv", "run", "pipecat-context-hub", "check-deprecation", symbol],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"check-deprecation {symbol} exited {result.returncode}: {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    failures: list[str] = []

    print("== CURRENT APIs (expect deprecated: false) ==")
    for symbol, why in CURRENT.items():
        verdict = _check(symbol)
        dep = bool(verdict.get("deprecated"))
        status = "FAIL" if dep else "ok  "
        if dep:
            failures.append(f"{symbol}: expected deprecated=false, got true ({why})")
        print(f"  [{status}] {symbol:<32} ({why})")

    print("\n== DEPRECATED APIs (expect deprecated: true) ==")
    for symbol, why in DEPRECATED.items():
        verdict = _check(symbol)
        dep = bool(verdict.get("deprecated"))
        status = "ok  " if dep else "FAIL"
        if not dep:
            failures.append(f"{symbol}: expected deprecated=true, got false ({why})")
        print(f"  [{status}] {symbol:<32} ({why})")

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll canaries passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
