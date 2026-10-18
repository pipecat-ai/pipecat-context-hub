"""The terminal output boundary (``shared/terminal.py``).

Contract, by output kind. Human text (stdout/stderr lines, log records,
exception messages, uncaught tracebacks): no raw non-printable character except
the program's own ``\\n``/``\\t``. Machine JSON on stdout: valid JSON that parses
to exactly the value it had, with no raw non-printable character except
structural ``\\n``/``\\t``. The tests pin each kind's enforcement point
(``echo``, ``echo_json``, the root group's click-error escaping, the record
class and the excepthook the bootstrap installs) and that no module bypasses
them. Every test starts with the stdlib record factory and excepthook
(``tests/conftest.py::_stdlib_output_hooks``).
"""

from __future__ import annotations

import ast
import json
import logging
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest

import pipecat_context_hub
from pipecat_context_hub.shared import env_loading, terminal
from pipecat_context_hub.shared.terminal import escape_controls, escape_json

HOSTILE = "evil\x1b]52;c;cHduZWQ=\x07\x1b[2J ‮\u200b\U000e0001"
_RAW = ("\x1b", "\x07", " ", "‮", "\u200b", "\U000e0001")


def _raw(text: str) -> bool:
    return any(ch in text for ch in _RAW)


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
    def test_the_bootstrap_installs_it(self, caplog: pytest.LogCaptureFixture) -> None:
        env_loading.load_env_layers()
        with caplog.at_level(logging.WARNING):
            logging.getLogger("pipecat_context_hub.any").warning("value: %s", HOSTILE)
        assert not any(_raw(r.getMessage()) for r in caplog.records)
        assert not _raw(caplog.text)
        assert f"value: {escape_controls(HOSTILE)}" in caplog.text

    def test_a_formatted_traceback_is_escaped(self) -> None:
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

    def test_last_resort_handler_output_is_escaped(self) -> None:
        """Entry points that never configure logging (the dashboard scripts) print
        load-time warnings through ``logging.lastResort``."""
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "%s", (HOSTILE,), None
        )
        assert logging.lastResort is not None
        assert not _raw(logging.lastResort.format(record))

    def test_install_is_idempotent(self) -> None:
        terminal.install_log_escaping()
        first = logging.getLogRecordFactory()
        terminal.install_log_escaping()
        assert logging.getLogRecordFactory() is first is terminal.TerminalSafeLogRecord

    def test_a_foreign_factory_is_kept_and_wrapped(self) -> None:
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

    def test_records_round_trip_through_make_log_record(self) -> None:
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, "m", (), None)
        record.exc_text = "trace"
        clone = logging.makeLogRecord(record.__dict__)
        assert clone.exc_text == "trace" and clone.getMessage() == "m"


class TestNoBareEcho:
    """``terminal.echo`` / ``terminal.echo_json`` are the only routes to
    ``click.echo``: a new bare call would bypass the boundary. ``print``/
    ``sys.stderr.write`` are allowed only where the text is the program's own
    (no configured value) or already escaped. JSON goes only through
    ``echo_json`` (the human escaper corrupts it), and nothing calls a click
    exception's ``show`` (the root group's escaping runs before click does)."""

    # Calls whose result is a JSON document.
    _JSON_PRODUCERS = {"dumps", "model_dump_json", "_mcp_json", "annotate_response"}

    _PRINT_ALLOWED = {
        # The boundary itself: its excepthook writes escaped text.
        "shared/terminal.py",
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

    @classmethod
    def _is_json(cls, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in cls._JSON_PRODUCERS

    # Modules whose sink spellings are checked, and the names each forbids
    # importing directly (``from click import echo as e`` included).
    _SINK_MODULES = {"click", "typer", "rich", "sys", "os"}
    _FORBIDDEN_FROM = {
        "click": {"echo", "secho"},
        "typer": {"echo", "secho"},
        "rich": {"print"},
        "sys": {"stdout", "stderr", "__stdout__", "__stderr__"},
        "os": {"write"},
    }
    _STREAMS = ("stdout", "stderr", "__stdout__", "__stderr__")

    @classmethod
    def _sink_offenders(cls, rel: str, tree: ast.AST) -> list[str]:
        """Bare sink calls in one module. A tripwire on spelling, not proof:
        plain ``import X as Y`` aliases are resolved, but a sink reached by
        ``getattr(click, "echo")``, a stream bound to a variable
        (``s = sys.stderr; s.write``) or a non-constant ``os.write`` fd is not
        seen, and the group test below sees only ``@x.group(...)`` on a plain
        ``def``."""
        aliases: dict[str, str] = {}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in cls._SINK_MODULES:
                        aliases[a.asname or a.name] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module in cls._FORBIDDEN_FROM:
                for a in node.names:
                    if a.name in cls._FORBIDDEN_FROM[node.module]:
                        offenders.append(f"{rel}: from {node.module} import {a.name}")
        for call in cls._calls(tree):
            func = call.func
            owner = (
                aliases.get(func.value.id, func.value.id)
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else None
            )
            if (
                owner in ("click", "typer")
                and isinstance(func, ast.Attribute)
                and func.attr in ("echo", "secho")
                and not (owner == "click" and rel == "shared/terminal.py")
            ):
                offenders.append(f"{rel}:{call.lineno} {owner}.{func.attr}")
            if owner == "rich" and isinstance(func, ast.Attribute) and func.attr == "print":
                offenders.append(f"{rel}:{call.lineno} rich.print")
            if isinstance(func, ast.Attribute) and func.attr == "show":
                offenders.append(f"{rel}:{call.lineno} .show()")
            if (
                isinstance(func, ast.Name)
                and func.id == "echo"
                and call.args
                and cls._is_json(call.args[0])
            ):
                offenders.append(f"{rel}:{call.lineno} echo(<json>): use echo_json")
            if rel in cls._PRINT_ALLOWED:
                continue
            if isinstance(func, ast.Name) and func.id == "print":
                offenders.append(f"{rel}:{call.lineno} print")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "write"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in cls._STREAMS
            ):
                offenders.append(f"{rel}:{call.lineno} sys.{func.value.attr}.write")
            if (
                owner == "os"
                and isinstance(func, ast.Attribute)
                and func.attr == "write"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in (1, 2)
            ):
                offenders.append(f"{rel}:{call.lineno} os.write({call.args[0].value!r}, ...)")
        return offenders

    def test_click_echo_is_called_only_by_the_boundary(self) -> None:
        root = Path(pipecat_context_hub.__file__).parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            offenders += self._sink_offenders(rel, ast.parse(path.read_text(encoding="utf-8")))
        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            "import click as c\nc.echo(x)",
            "import typer\ntyper.echo(x)",
            "import rich\nrich.print(x)",
            "from rich import print\n",
            "from click import echo as e\n",
            "import sys\nsys.__stderr__.write(x)",
            "import sys as s\ns.__stdout__.write(x)",
            "import os\nos.write(2, b'x')",
            "import os as o\no.write(1, b'x')",
        ],
    )
    def test_the_guard_sees_each_sink_spelling(self, source: str) -> None:
        assert self._sink_offenders("mod.py", ast.parse(source))


# Non-printable characters plausible in indexed docs text or a configured
# value; each is invalid JSON once the human escaper rewrites it (\xNN, \U...)
# or would reach the terminal raw.
_JSON_HOSTILE = ["\xa0", "\xad", "\x7f", "\x85", "\x1b", " ", "　", "\U000e0001"]


def _has_raw_non_printable(text: str) -> bool:
    return any(not (ch in "\n\t" or ch.isprintable()) for ch in text)


class TestEscapeJson:
    """Machine JSON: valid, same value, no raw non-printable character."""

    def test_printable_json_is_byte_identical(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            s: str

        compact = M(s="é ✓ \\x1b 'q'").model_dump_json()
        indented = json.dumps({"a": ["é", 1, None]}, indent=2, ensure_ascii=False)
        assert escape_json(compact) == compact
        assert escape_json(indented) == indented

    @pytest.mark.parametrize("ch", _JSON_HOSTILE, ids=[f"U+{ord(c):04X}" for c in _JSON_HOSTILE])
    @pytest.mark.parametrize("producer", ["pydantic", "dumps", "dumps-ascii"])
    def test_parses_to_the_same_value_with_nothing_raw(self, ch: str, producer: str) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            s: str
            items: list[str]

        text, items = f"a{ch}b", [f"{ch}", f'x\\{ch}"y']
        value: dict[str, Any] = {"s": text, "items": items}
        if producer == "pydantic":
            document = M(s=text, items=items).model_dump_json()
        else:
            document = json.dumps(value, indent=2, ensure_ascii=producer == "dumps-ascii")
        out = escape_json(document)
        assert json.loads(out) == json.loads(document) == value
        assert not _has_raw_non_printable(out)
        assert escape_json(out) == out

    def test_structural_carriage_return_keeps_the_value(self) -> None:
        document = '{"a":\r\n 1}'
        out = escape_json(document)
        assert json.loads(out) == {"a": 1}
        assert not _has_raw_non_printable(out)

    def test_echo_json_prints_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        document = json.dumps({"s": HOSTILE}, ensure_ascii=False)
        terminal.echo_json(document)
        out = capsys.readouterr().out
        assert out.endswith("\n")
        assert json.loads(out) == {"s": HOSTILE}
        assert not _raw(out)


def _restore_reducing_record(state: dict[str, Any]) -> logging.LogRecord:
    """``ReducingRecord``'s own reconstructor; marks that it was the one used."""
    record = TestRecordReadSideEscaping.ReducingRecord.__new__(
        TestRecordReadSideEscaping.ReducingRecord
    )
    record.__dict__.update(state, restored_by_host=True)
    return record


class TestRecordReadSideEscaping:
    """Message, traceback text and stack text are escaped when a formatter reads
    them, whichever factory made the record and however the text got there."""

    _FORMAT = logging.Formatter("%(levelname)s %(message)s")

    class ForeignRecord(logging.LogRecord):
        def getMessage(self) -> str:  # noqa: N802
            return "foreign:" + super().getMessage()

    @staticmethod
    def _foreign_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return TestRecordReadSideEscaping.ForeignRecord(*args, **kwargs)

    def _install(self, factory: str) -> None:
        if factory == "foreign":
            logging.setLogRecordFactory(self._foreign_factory)
        terminal.install_log_escaping()

    @pytest.mark.parametrize("factory", ["default", "foreign"])
    def test_hostile_record_fields_are_escaped(self, factory: str) -> None:
        self._install(factory)
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "msg %s", (HOSTILE,), None
        )
        record.exc_text = f"Traceback {HOSTILE}"
        record.stack_info = f"Stack {HOSTILE}"
        text = self._FORMAT.format(record)
        assert not _raw(text)
        for part in (f"msg {escape_controls(HOSTILE)}", f"Traceback {escape_controls(HOSTILE)}"):
            assert part in text
        assert f"Stack {escape_controls(HOSTILE)}" in text
        # Stored raw, so a record round-trips unchanged.
        assert record.__dict__["exc_text"] == f"Traceback {HOSTILE}"

    @pytest.mark.parametrize("factory", ["default", "foreign"])
    def test_make_log_record_bypassing_the_setters_is_escaped(self, factory: str) -> None:
        """``makeLogRecord`` writes ``__dict__`` directly, past any setter."""
        self._install(factory)
        record = logging.makeLogRecord(
            {
                "msg": f"m {HOSTILE}",
                "exc_text": f"Traceback {HOSTILE}",
                "stack_info": f"Stack {HOSTILE}",
            }
        )
        text = self._FORMAT.format(record)
        assert not _raw(text)
        assert f"Traceback {escape_controls(HOSTILE)}" in text
        assert f"Stack {escape_controls(HOSTILE)}" in text

    @pytest.mark.parametrize("factory", ["default", "foreign"])
    def test_a_real_traceback_is_escaped(self, factory: str) -> None:
        self._install(factory)
        try:
            raise OSError(f"cannot open {HOSTILE}")
        except OSError:
            record = logging.getLogger("t").makeRecord(
                "t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        text = self._FORMAT.format(record)
        assert "Traceback" in text and not _raw(text)

    @pytest.mark.parametrize("factory", ["default", "foreign"])
    def test_message_formatting_stays_lazy(self, factory: str) -> None:
        """A bad %-call must reach ``Handler.handleError``, not raise at the call."""
        self._install(factory)
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "%s %s", ("only-one",), None
        )
        with pytest.raises(TypeError):
            record.getMessage()

    @pytest.mark.parametrize("factory", ["default", "foreign"])
    def test_records_survive_a_pickle_round_trip(self, factory: str) -> None:
        """A host's ``QueueHandler`` over a ``multiprocessing.Queue`` pickles
        each record; a re-classed foreign record must pickle like the host's."""
        import pickle

        self._install(factory)
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "msg %s", (HOSTILE,), None
        )
        record.exc_text = f"Traceback {HOSTILE}"
        clone = pickle.loads(pickle.dumps(record))
        assert type(clone) is type(record)
        assert clone.__dict__ == record.__dict__
        assert clone.getMessage() == record.getMessage()
        assert not _raw(self._FORMAT.format(clone))

    class SlottedRecord(logging.LogRecord):
        __slots__ = ("req_id",)

    class ReducingRecord(logging.LogRecord):
        """A host class with its own reduction, which must stay the one used."""

        def __reduce__(self) -> Any:
            return _restore_reducing_record, (self.__dict__.copy(),)

    @pytest.mark.parametrize("copier", ["pickle", "copy", "deepcopy"])
    @pytest.mark.parametrize("host", ["slotted", "reducing"])
    def test_a_foreign_record_copies_like_the_host_class(self, host: str, copier: str) -> None:
        """Copying a re-classed record (``QueueHandler.prepare`` copies, a
        ``multiprocessing.Queue`` pickles) keeps slot state and honours the host
        class's own ``__reduce__``, exactly as without the escaping."""
        import copy
        import pickle

        cls = self.SlottedRecord if host == "slotted" else self.ReducingRecord
        logging.setLogRecordFactory(cls)
        terminal.install_log_escaping()
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "msg %s", (HOSTILE,), None
        )
        if host == "slotted":
            record.req_id = "R42"
        if copier == "pickle":
            clone = pickle.loads(pickle.dumps(record))
        else:
            clone = getattr(copy, copier)(record)
        assert type(clone) is type(record)
        assert isinstance(clone, cls) and isinstance(clone, terminal.TerminalSafeLogRecord)
        if host == "slotted":
            assert clone.req_id == "R42"
        else:
            assert clone.__dict__.pop("restored_by_host") is True
        assert clone.__dict__ == record.__dict__
        assert not _raw(self._FORMAT.format(clone))

    def test_a_foreign_record_keeps_its_class_behaviour(self) -> None:
        self._install("foreign")
        record = logging.getLogger("t").makeRecord(
            "t", logging.WARNING, __file__, 1, "%s", (HOSTILE,), None
        )
        assert isinstance(record, self.ForeignRecord)
        assert isinstance(record, terminal.TerminalSafeLogRecord)
        assert record.getMessage() == escape_controls(f"foreign:{HOSTILE}")

    def test_a_second_install_does_not_wrap_twice(self) -> None:
        self._install("foreign")
        wrapper = logging.getLogRecordFactory()
        assert wrapper is not self._foreign_factory
        terminal.install_log_escaping()
        assert logging.getLogRecordFactory() is wrapper


class TestExcepthook:
    def test_uncaught_traceback_is_escaped(self, capsys: pytest.CaptureFixture[str]) -> None:
        terminal.install_output_escaping()
        assert sys.excepthook is terminal.terminal_safe_excepthook
        try:
            raise OSError(f"cannot open {HOSTILE}")
        except OSError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        err = capsys.readouterr().err
        assert "Traceback" in err and "OSError" in err
        assert not _raw(err)
        assert escape_controls(HOSTILE) in err

    def test_printable_traceback_matches_the_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        try:
            raise ValueError("plain")
        except ValueError as exc:
            terminal.terminal_safe_excepthook(type(exc), exc, exc.__traceback__)
            expected = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert capsys.readouterr().err == expected

    def test_a_host_hook_is_left_alone(self) -> None:
        def host(*args: Any) -> None:
            pass

        sys.excepthook = host
        terminal.install_output_escaping()
        assert sys.excepthook is host


class TestClickErrors:
    """Click shows a ``ClickException`` itself; the root group escapes it first."""

    def test_reset_index_refusal_naming_a_hostile_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproducer: a cwd ``.env`` points PIPECAT_HUB_DATA_DIR at the home
        directory through a component carrying OSC/BEL; the refusal names it."""
        from click.testing import CliRunner

        from pipecat_context_hub.cli import main

        home = tmp_path / "home"
        project = tmp_path / "proj"
        home.mkdir()
        project.mkdir()
        (project / ".env").write_text(
            "PIPECAT_HUB_DATA_DIR=../home/\x1b]0;PWNED\x07/..\n", encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(project)
        result = CliRunner().invoke(main, ["refresh", "--reset-index"])
        assert result.exit_code == 1
        assert "Refusing to delete" in result.stderr
        assert "\x1b" not in result.output and "\x07" not in result.output
        assert "[invalid]" in result.stderr

    def test_a_command_s_click_exception_is_escaped(self) -> None:
        from click.testing import CliRunner

        from pipecat_context_hub.cli import TerminalSafeGroup

        @click.group(cls=TerminalSafeGroup)
        def root() -> None:
            pass

        @root.command()
        def boom() -> None:
            raise click.ClickException(f"bad {HOSTILE}")

        result = CliRunner().invoke(root, ["boom"])
        assert result.exit_code == 1
        assert result.stderr == f"Error: bad {escape_controls(HOSTILE)}\n"

    def test_a_usage_error_from_parsing_is_escaped(self) -> None:
        from click.testing import CliRunner

        from pipecat_context_hub.cli import main

        result = CliRunner().invoke(main, [f"no-such-{HOSTILE}"])
        assert result.exit_code == 2
        assert "No such command" in result.stderr
        assert not _raw(result.output)

    def test_both_front_doors_share_the_safe_root(self) -> None:
        from pipecat_context_hub import plugin
        from pipecat_context_hub.cli import TerminalSafeGroup, main

        assert isinstance(main, TerminalSafeGroup)
        assert getattr(plugin, "hub_cli") is main  # noqa: B009 - not re-exported

    def test_every_group_in_src_is_safe_or_under_the_safe_root(self) -> None:
        """A group outside the root would show its own errors raw."""
        import importlib

        from pipecat_context_hub.cli import TerminalSafeGroup, main

        reachable: list[click.Command] = []
        pending: list[click.Command] = [main]
        while pending:
            command = pending.pop()
            reachable.append(command)
            if isinstance(command, click.Group):
                pending.extend(command.commands.values())

        root = Path(pipecat_context_hub.__file__).parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    if not (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "group"
                    ):
                        continue
                    module = importlib.import_module(
                        "pipecat_context_hub." + rel.removesuffix(".py").replace("/", ".")
                    )
                    group = getattr(module, node.name)
                    if not isinstance(group, TerminalSafeGroup) and group not in reachable:
                        offenders.append(f"{rel}:{node.lineno} {node.name}")
        assert offenders == []
