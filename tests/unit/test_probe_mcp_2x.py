"""Regression tests for the MCP SDK compatibility probe matrix."""

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[2] / "scripts" / "probe_mcp_2x.py"
_SPEC = importlib.util.spec_from_file_location("probe_mcp_2x", _SCRIPT)
assert _SPEC and _SPEC.loader
_PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PROBE)


def test_matrix_excludes_the_project_from_both_sdk_resolutions():
    for requirement in ("mcp==2.0.*", "mcp>=2,<3"):
        command = _PROBE._matrix_command(_SCRIPT, requirement)

        assert command[command.index("--isolated") + 1] == "--no-project"
        assert requirement in command
