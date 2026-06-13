#!/usr/bin/env python3
"""Live-hub smoke test for ``check_deprecation`` false/true positives.

Runs the ``check-deprecation`` CLI subcommand against the **built local index**
for a curated set of canary symbols (AGENTS.md item #48) and asserts the
``deprecated`` verdict. Unlike the offline fixture smoke under ``tests/smoke/``,
this hits the real persisted ``deprecation_map.json`` and therefore requires a
prior ``refresh`` — it is intentionally NOT part of the pytest gate.

Two directions are checked:

* CURRENT — current, non-deprecated APIs that must return ``deprecated: false``.
  A false positive here (current API flagged deprecated) is the worst failure
  mode the tool has. Includes the owner-of-member cases fixed alongside this
  script (colon header, possessive, adjacent, member-for, For-subject, and
  delimiter-list owners after a preposition).
* DEPRECATED — genuinely deprecated/removed APIs that must stay
  ``deprecated: true``. Guards against the owner-context skip over-reaching and
  silently dropping real deprecations.

Usage::

    uv run python scripts/smoke_check_deprecation.py          # full run
    uv run python scripts/smoke_check_deprecation.py --known-gaps   # also report deferred Gap-D FPs

Exit codes: 0 all canaries pass, 1 one or more regressions.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 — hardcoded CLI args, no user input
import sys

# Current APIs — must report deprecated: false. Each is annotated with the
# release-note phrasing class that previously produced a false positive.
CURRENT: dict[str, str] = {
    # Stable core classes — version-independent regression canaries.
    "Pipeline": "core class",
    "CartesiaTTSService": "core class",
    "SileroVADAnalyzer": "core class",
    # Owner-of-member phrasings (fixed).
    "DeepgramSTTService": "`X`: `member` colon header",
    "GladiaSTTService": "`X`'s `member` possessive",
    "SimliVideoService": "`X` `member` parameter adjacent",
    "MiniMaxHttpTTSService": "`member` parameter for `X`",
    "SpeechmaticsSTTService": "For `X`, the `member` …",
    "StartFrame": "delimiter-list owner after preposition",
    "FrameProcessor": "delimiter-list owner after preposition",
    "CartesiaHttpTTSService": "delimiter-list owner after preposition",
    "InputParams": "colon-header nested class member",
}

# Genuinely deprecated/removed APIs — must report deprecated: true.
DEPRECATED: dict[str, str] = {
    "PipelineTask": "renamed to PipelineWorker (1.3.0)",
    "STTMuteFilter": "removed class (list before preposition)",
    "UserResponseAggregator": "removed class (with 'class' noun)",
    "EmulateUserStoppedSpeakingFrame": "removed frame",
    "NimLLMService": "deprecated, use NvidiaLLMService",
    "SambaNovaSTTService": "removed service",
}

# Deferred "replacement-kept" (Gap D) false positives — still mis-keyed,
# documented in AGENTS.md #48. Reported (not asserted) under --known-gaps.
KNOWN_GAP_FPS: dict[str, str] = {
    "OpenAILLMService": "can still be used with `OpenAILLMService`",
    "WebsocketTTSService": "Subclass `WebsocketTTSService` directly",
    "TTSService": "part of the base `TTSService` (0.0.105 bullet)",
    "LLMContext": "now built into `LLMContext`",
    "LocalSmartTurnAnalyzerV3": "`transformers` now always installed",
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
    parser.add_argument(
        "--known-gaps",
        action="store_true",
        help="also report the deferred Gap-D replacement-kept false positives",
    )
    args = parser.parse_args()

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

    if args.known_gaps:
        print("\n== KNOWN-GAP false positives (Gap D — not yet fixed, informational) ==")
        for symbol, why in KNOWN_GAP_FPS.items():
            verdict = _check(symbol)
            dep = bool(verdict.get("deprecated"))
            mark = "still-FP" if dep else "resolved"
            print(f"  [{mark}] {symbol:<32} ({why})")

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll canaries passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
