#!/usr/bin/env python3
"""Live-hub smoke test for ``check_deprecation`` false/true positives.

Runs the ``check-deprecation`` CLI subcommand against the **built local index**
for a curated set of canary symbols (docs/smoke-test.md items #34–37, #46–48) and asserts
the ``deprecated`` verdict. Unlike the offline fixture smoke under
``tests/smoke/``, this hits the real persisted ``deprecation_map.json``, built
from pipecat's deprecation registry (PR #85), and therefore requires a prior
``refresh`` — it is intentionally NOT part of the pytest gate.

Three groups are checked:

* CURRENT — current, non-deprecated APIs that must return ``deprecated: false``.
  A false positive here (current API flagged deprecated) is the worst failure
  mode the tool has. Includes ancestor packages and owner-of-member classes,
  which forward-prefix matching must NOT flag.
* DEPRECATED — genuinely deprecated APIs that must stay ``deprecated: true``.
  Guards against the lookup over-narrowing and silently dropping real
  deprecations.
* HINT — bare deprecated member names: ``deprecated: false``, ``status: null`` and
  exactly the expected candidate (symbol and replacement, from ``HINT_EXPECTED``): a
  hint, not a verdict; a clean ``current`` or a wrong candidate is a miss.

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


# Bare member names — must return a hint: deprecated false, status null, candidates.
HINT: dict[str, str] = {
    "trigger_user_turn_stopped": "bare deprecated member -> report_user_turn_stopped",
}

# The one candidate each HINT name must list: (candidate symbol, replacement).
HINT_EXPECTED: dict[str, tuple[str, str]] = {
    "trigger_user_turn_stopped": (
        "BaseUserTurnStopStrategy.trigger_user_turn_stopped",
        "report_user_turn_stopped",
    ),
}


def classify(
    response: dict[str, object],
    expected_group: str,
    expected_candidate: tuple[str, str] | None = None,
) -> str | None:
    """Return a failure description, or None when ``response`` fits ``expected_group``.

    ``expected_group`` is ``"current"``, ``"deprecated"`` or ``"hint"``. The
    ``"hint"`` group also needs ``expected_candidate``, the ``(symbol, replacement)``
    of the single candidate the hint must list.
    """
    dep = bool(response.get("deprecated"))
    status = response.get("status")
    candidates = response.get("candidates")
    if expected_group == "current":
        if dep:
            return "expected deprecated=false, got true"
        if status != "current":
            return f"expected status='current', got {status!r}"
        if candidates:
            return "expected no candidates, got some"
        return None
    if expected_group == "deprecated":
        return None if dep else "expected deprecated=true, got false"
    if expected_group == "hint":
        for key in ("deprecated", "status", "candidates", "candidates_total"):
            if key not in response:
                return f"missing key {key!r}"
        if dep:
            return "expected deprecated=false, got true"
        if status is not None:
            return f"expected status=null, got {status!r}"
        if expected_candidate is None:
            raise ValueError("the hint group needs expected_candidate")
        if not isinstance(candidates, list) or len(candidates) != 1:
            return f"expected exactly one candidate, got {candidates!r}"
        if response["candidates_total"] != 1:
            return f"expected candidates_total=1, got {response['candidates_total']!r}"
        (cand,) = candidates
        got = (cand.get("symbol"), cand.get("replacement")) if isinstance(cand, dict) else None
        if got != expected_candidate:
            return f"expected candidate {expected_candidate!r}, got {got!r}"
        return None
    raise ValueError(f"unknown group {expected_group!r}")


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

    groups = (
        ("CURRENT APIs (expect deprecated: false, status: current)", "current", CURRENT),
        ("DEPRECATED APIs (expect deprecated: true)", "deprecated", DEPRECATED),
        ("HINT names (expect deprecated: false, status: null, candidates)", "hint", HINT),
    )
    for i, (title, group, symbols) in enumerate(groups):
        print(("\n" if i else "") + f"== {title} ==")
        for symbol, why in symbols.items():
            problem = classify(_check(symbol), group, HINT_EXPECTED.get(symbol))
            if problem:
                failures.append(f"{symbol}: {problem} ({why})")
            print(f"  [{'FAIL' if problem else 'ok  '}] {symbol:<32} ({why})")

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll canaries passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
