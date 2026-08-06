"""GitHub issue-template URLs shared by the MCP instructions and the CLI.

Single source of truth for the two report-hint destinations so the MCP
server's ``_SERVER_INSTRUCTIONS`` and the CLI's stderr hints cannot drift
apart on the URL itself. A `tests/unit/test_server.py::TestSupportLinks`
guard walks ``src/**/*.py`` to enforce that no other module hardcodes an
``issues/new?template=`` literal.
"""

from __future__ import annotations

RETRIEVAL_QUALITY_ISSUE_URL = (
    "https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml"
)

BUG_REPORT_ISSUE_URL = (
    "https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=bug-report.yml"
)


def bug_report_hint() -> str:
    """Remediation-first suffix: name the fix, then the tracker as a fallback.

    Precondition: the caller has already emitted its own remediation text
    (e.g. "run 'refresh --force --reset-index'") immediately before this
    string. This function only returns the trailing "if this persists..."
    clause — it names no fix of its own — so appending it in isolation, with
    no preceding remediation, reads as escalation-first rather than the
    remediation-first ordering every call site relies on. Every current call
    site (index-unready messages, the reranker not_cached warning) upholds
    this by construction; keep that invariant when adding a new one.
    """
    return f"If this persists after trying that, file a bug report at {BUG_REPORT_ISSUE_URL}."
