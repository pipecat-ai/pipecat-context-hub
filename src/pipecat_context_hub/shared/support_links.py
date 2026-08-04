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
