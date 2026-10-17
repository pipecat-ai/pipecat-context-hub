"""The terminal output boundary (``shared/terminal.py``).

Contract: no configured string reaches stdout, stderr or log output with a raw
non-printable character except the program's own ``\\n``/``\\t``. The tests
here pin the two enforcement points (``echo`` and the log record factory the
bootstrap installs) and that no module bypasses ``echo``.
"""

from __future__ import annotations

import ast
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import pipecat_context_hub
from pipecat_context_hub.shared import env_loading, terminal
from pipecat_context_hub.shared.terminal import escape_controls

HOSTILE = "evil\x1b]52;c;cHduZWQ=\x07\x1b[2J ‮\u200b\U000e0001"
_RAW = ("\x1b", "\x07", " ", "‮", "\u200b", "\U000e0001")


def _raw(text: str) -> bool:
    return any(ch in text for ch in _RAW)


@pytest.fixture
def default_record_factory() -> Iterator[None]:
    """Run with the stdlib record factory, so a test proves who installs the boundary."""
    previous = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    try:
        yield
    finally:
        logging.setLogRecordFactory(previous)


class TestEscapeControls:
    def test_printable_text_is_unchanged(self) -> None:
        text = "ignoring ~/cfg/config.toml: it is world-writable — é ✓ 'x' \\x1b"
        assert escape_controls(text) == text

    def test_layout_newlines_and_tabs_are_kept(self) -> None:
        assert escape_controls("a\n\tb") == "a\n\tb"

    def test_each_non_printable_character_is_escaped(self) -> None:
        assert escape_controls("a\x1bb\rc d\U000e0001") == ("a\\x1bb\\x0dc\\u2028d\\U000e0001")

    def test_output_is_printable_and_idempotent(self) -> None:
        out = escape_controls(HOSTILE)
        assert out.isprintable()
        assert escape_controls(out) == out


class TestEcho:
    def test_echo_escapes_stdout_and_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.echo(f"Added {HOSTILE}\nnext")
        terminal.echo(f"Error: {HOSTILE}", err=True)
        out, err = capsys.readouterr()
        assert not _raw(out) and not _raw(err)
        assert out == f"Added {escape_controls(HOSTILE)}\nnext\n"
        assert err == f"Error: {escape_controls(HOSTILE)}\n"

    def test_echo_with_no_message_prints_a_blank_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        terminal.echo()
        assert capsys.readouterr().out == "\n"


class TestLogBoundary:
    def test_the_bootstrap_installs_it(
        self, default_record_factory: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        env_loading.load_env_layers()
        with caplog.at_level(logging.WARNING):
            logging.getLogger("pipecat_context_hub.any").warning("value: %s", HOSTILE)
        assert not any(_raw(r.getMessage()) for r in caplog.records)
        assert not _raw(caplog.text)
        assert f"value: {escape_controls(HOSTILE)}" in caplog.text

    def test_a_formatted_traceback_is_escaped(self, default_record_factory: None) -> None:
        terminal.install_log_escaping()
        try:
            raise OSError(f"cannot open {HOSTILE}")
        except OSError:
            record = logging.getLogger("t").makeRecord(
                "t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        text = logging.Formatter("%(levelname)s %(message)s").format(record)
        assert "Traceback" in text and "\n" in text
        assert not _raw(text)
        assert escape_controls(HOSTILE) in text

    def test_last_resort_handler_output_is_escaped(self, default_record_factory: None) -> None:
        """Entry points that never configure logging (the dashboard scripts) print
        load-time warnings through ``logging.lastResort``."""
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "%s", (HOSTILE,), None
        )
        assert logging.lastResort is not None
        assert not _raw(logging.lastResort.format(record))

    def test_install_is_idempotent(self, default_record_factory: None) -> None:
        terminal.install_log_escaping()
        first = logging.getLogRecordFactory()
        terminal.install_log_escaping()
        assert logging.getLogRecordFactory() is first is terminal.TerminalSafeLogRecord

    def test_a_foreign_factory_is_kept_and_wrapped(self, default_record_factory: None) -> None:
        seen: list[str] = []

        def foreign(*args: Any, **kwargs: Any) -> logging.LogRecord:
            seen.append("called")
            return logging.LogRecord(*args, **kwargs)

        logging.setLogRecordFactory(foreign)
        terminal.install_log_escaping()
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "%s", (HOSTILE,), None
        )
        assert seen == ["called"]
        assert not _raw(record.getMessage())

    def test_records_round_trip_through_make_log_record(self, default_record_factory: None) -> None:
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, "m", (), None)
        record.exc_text = "trace"
        clone = logging.makeLogRecord(record.__dict__)
        assert clone.exc_text == "trace" and clone.getMessage() == "m"


class TestNoBareEcho:
    """``terminal.echo`` is the only route to ``click.echo``: a new bare call
    would bypass the boundary. ``print``/``sys.stderr.write`` are allowed only
    where the text is the program's own (no configured value)."""

    _PRINT_ALLOWED = {
        # Fixed text plus a numeric constant.
        "server/transport.py",
        # A dev-only generator printing repo-relative paths of tracked files.
        "shared/repo_catalog_render.py",
    }

    @staticmethod
    def _calls(tree: ast.AST) -> Iterator[ast.Call]:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield node

    def test_click_echo_is_called_only_by_the_boundary(self) -> None:
        root = Path(pipecat_context_hub.__file__).parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "click":
                    if any(a.name in ("echo", "secho") for a in node.names):
                        offenders.append(f"{rel}: from click import echo")
            for call in self._calls(tree):
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("echo", "secho")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "click"
                    and rel != "shared/terminal.py"
                ):
                    offenders.append(f"{rel}:{call.lineno} click.{func.attr}")
                if rel in self._PRINT_ALLOWED:
                    continue
                if isinstance(func, ast.Name) and func.id == "print":
                    offenders.append(f"{rel}:{call.lineno} print")
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "write"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr in ("stdout", "stderr")
                ):
                    offenders.append(f"{rel}:{call.lineno} sys.{func.value.attr}.write")
        assert offenders == []
