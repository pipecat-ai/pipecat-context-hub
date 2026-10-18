"""Terminal-safe text: the one output boundary for configured strings.

Contract, by output kind:

* **Human text** (stdout/stderr lines, log records, exception messages and
  uncaught tracebacks): no non-printable character (``str.isprintable()``
  False: controls, format characters, line/paragraph separators, NBSP and the
  like) reaches the terminal raw, except the ``\\n``/``\\t`` of the program's
  own layout.
* **Machine JSON on stdout** (the query commands' tool output, ``install
  --print-config``): stays valid JSON that parses to exactly the value it had,
  and carries no raw non-printable character except structural ``\\n``/``\\t``.
  The human escaper's ``\\xNN``/``\\UNNNNNNNN`` are not JSON escapes, so JSON
  never goes through it.

Configured values are never validated on the way in (a project ``.env`` can set
``PIPECAT_HUB_CONFIG_FILE``, ``PIPECAT_HUB_DATA_DIR`` or the repo lists), so they
can carry terminal escape sequences (OSC 52, CSI), line separators or bidi
controls. Each kind has one enforcement point, at the sink:

* :func:`echo` (human) and :func:`echo_json` (JSON) are the only
  ``click.echo`` calls in ``src/`` (a source test pins it).
* Click renders a ``ClickException`` (``UsageError``, ``BadParameter``...)
  itself, via ``ClickException.show``. The hub's root group
  (``cli.TerminalSafeGroup``) runs its parsing and dispatch inside
  :func:`escaped_click_errors`, which escapes the exception's message before
  click shows it; ``plugin.py``'s bridge calls that same group's ``main``.
* :func:`install_output_escaping` makes every log record terminal-safe (its
  message, cached traceback ``exc_text`` and ``stack_info`` are escaped when a
  formatter *reads* them, so every handler, ``logging.lastResort`` and
  ``logging.makeLogRecord`` included, sees escaped text) and, when no host has
  replaced it, wraps ``sys.excepthook`` so an uncaught traceback is escaped.
  ``shared/env_loading.load_env_layers`` (the bootstrap every entry point calls
  first) installs it, before any load-time warning. ``plugin.py``'s bridge
  prints an uncaught exception through :func:`terminal_safe_excepthook` itself,
  since the host's Typer installs its own excepthook first.

Stated limits: a ``\\n`` inside a configured value is kept like the program's
own layout (a ``.env`` value cannot hold one; ``config.toml`` and the real
environment are owner-trusted). Escaping is applied where a formatter reads
the record, so a handler must render ``%(message)s`` (the hub's own do), not
``record.msg``/``record.args`` directly.

:func:`shown` is a presentation rule on top, not the safety mechanism: it flags
one configured *value* (a slug or a path) as ``repr() [invalid]`` where a
message names it, so the user sees which value is wrong.

A leaf: stdlib only at import (``click`` is imported where used), nothing from
the package (a layering test pins it).
"""

from __future__ import annotations

import contextlib
import logging
import sys
import traceback
from collections.abc import Iterator
from types import TracebackType
from typing import Any

_KEPT = frozenset("\n\t")


def _is_kept(ch: str) -> bool:
    return ch in _KEPT or ch.isprintable()


def escape_controls(text: str) -> str:
    """``text`` with every non-printable character except ``\\n``/``\\t`` escaped.

    For human text only (never JSON: see :func:`escape_json`). Each such
    character becomes ``\\xNN``, ``\\uNNNN`` or ``\\UNNNNNNNN``; all other text
    is byte-identical. The output's added characters are printable, so applying
    this twice changes nothing further.
    """
    if text.isprintable():
        return text
    out: list[str] = []
    for ch in text:
        if _is_kept(ch):
            out.append(ch)
            continue
        code = ord(ch)
        if code <= 0xFF:
            out.append(f"\\x{code:02x}")
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def _json_unicode_escape(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def escape_json(text: str) -> str:
    """JSON text with every non-printable character re-escaped, value unchanged.

    ``text`` is a JSON document (a tool's ``model_dump_json()``, a
    ``json.dumps``). Inside a string, each non-printable character becomes the
    JSON escape ``\\uXXXX`` (a surrogate pair beyond the BMP), so
    ``json.loads(escape_json(t)) == json.loads(t)``. Outside strings the only
    non-printable characters valid JSON can hold are structural whitespace:
    ``\\n``/``\\t`` are kept and ``\\r`` becomes a space (same value). Text that
    is not JSON still never carries a raw non-printable character out: anything
    else outside a string gets :func:`escape_controls`' escape. Printable JSON
    is byte-identical; applying this twice changes nothing further.
    """
    if all(_is_kept(ch) for ch in text):
        return text
    out: list[str] = []
    in_string = False
    backslash = False
    for ch in text:
        if in_string:
            if backslash:
                backslash = False
            elif ch == "\\":
                backslash = True
            elif ch == '"':
                in_string = False
            if ch.isprintable():
                out.append(ch)
            else:
                out.append(_json_unicode_escape(ch))
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        elif _is_kept(ch):
            out.append(ch)
        elif ch == "\r":
            out.append(" ")
        else:
            out.append(escape_controls(ch))
    return "".join(out)


def shown(text: str) -> str:
    """A configured value (slug or path), flagged when it is not printable.

    Text with any non-printable character (``str.isprintable()``: every
    control, format and separator character, newline included) prints as its
    ``repr()`` tagged ``[invalid]``; anything else, which includes every
    well-formed slug and path, as is. ``repr()`` output is itself printable, so
    applying this twice changes nothing further.
    """
    return text if text.isprintable() else f"{text!r} [invalid]"


def echo(message: object = None, *, err: bool = False, nl: bool = True) -> None:
    """``click.echo`` of human text, passed whole through :func:`escape_controls`."""
    import click  # deferred: keeps this leaf stdlib-only at import

    click.echo(None if message is None else escape_controls(str(message)), err=err, nl=nl)


def echo_json(document: str) -> None:
    """``click.echo`` of a JSON document to stdout, through :func:`escape_json`."""
    import click  # deferred: keeps this leaf stdlib-only at import

    click.echo(escape_json(document))


@contextlib.contextmanager
def escaped_click_errors() -> Iterator[None]:
    """Escape the message of a ``ClickException`` raised in the block.

    Click prints the exception itself (``ClickException.show``, after the
    command returns), so the message is made terminal-safe here, before it
    leaves the block. The rest of what ``show`` prints (usage line, ``Try
    --help`` hint) is the program's own text.
    """
    import click  # deferred: keeps this leaf stdlib-only at import

    try:
        yield
    except click.ClickException as exc:
        # Typed Final in click's stubs; a plain attribute at runtime, and the
        # one value every ClickException subclass's format_message renders.
        exc.message = escape_controls(exc.message)  # type: ignore[misc]
        raise


class TerminalSafeLogRecord(logging.LogRecord):
    """A record whose message, traceback text and stack text are escaped on read.

    ``getMessage`` is what every ``Formatter`` renders (it stays lazy, so a bad
    ``%`` call still surfaces through ``Handler.handleError``); ``exc_text`` is
    where a ``Formatter`` caches the traceback it appends and ``stack_info`` the
    stack it appends (either can embed a configured path). Both are escaped when
    *read*, and stored raw in ``__dict__`` under their usual names, so a value
    written by ``makeLogRecord``'s ``__dict__.update`` or by unpickling is
    covered too, and records round-trip unchanged.
    """

    def getMessage(self) -> str:  # noqa: N802 - logging's API name
        return escape_controls(super().getMessage())

    @property
    def exc_text(self) -> str | None:
        value: str | None = self.__dict__.get("exc_text")
        return None if value is None else escape_controls(value)

    @exc_text.setter
    def exc_text(self, value: str | None) -> None:
        self.__dict__["exc_text"] = value

    @property
    def stack_info(self) -> str | None:
        value: str | None = self.__dict__.get("stack_info")
        return None if value is None else escape_controls(value)

    @stack_info.setter
    def stack_info(self, value: str | None) -> None:
        self.__dict__["stack_info"] = value


_SAFE_CLASSES: dict[type[logging.LogRecord], type[logging.LogRecord]] = {}


def _rebuild_record(base: type[logging.LogRecord], state: dict[str, Any]) -> logging.LogRecord:
    """Unpickle a re-classed foreign record: ``base`` plus the escaping, ``state``."""
    safe = _safe_class(base)
    record = safe.__new__(safe)
    record.__dict__.update(state)
    return record


def _reduce_foreign(self: logging.LogRecord, protocol: int) -> Any:
    # The re-classed type is built at runtime and has no importable name, so it
    # pickles by the host's own class instead (which must be importable anyway).
    return _rebuild_record, (type(self).__bases__[1], self.__dict__.copy())


def _safe_class(cls: type[logging.LogRecord]) -> type[logging.LogRecord]:
    """``cls`` with :class:`TerminalSafeLogRecord`'s behaviour mixed in (cached)."""
    if issubclass(cls, TerminalSafeLogRecord):
        return cls
    if cls is logging.LogRecord:
        return TerminalSafeLogRecord
    safe = _SAFE_CLASSES.get(cls)
    if safe is None:
        safe = type(
            f"TerminalSafe{cls.__name__}",
            (TerminalSafeLogRecord, cls),
            {"__reduce_ex__": _reduce_foreign},
        )
        _SAFE_CLASSES[cls] = safe
    return safe


def _escaping_wrapper(previous: Any) -> Any:
    """Wrap a foreign record factory: its records get the same read-side escaping.

    Each record is re-classed to its class plus :class:`TerminalSafeLogRecord`
    (nothing is formatted here), so the default and foreign paths behave alike.
    """

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record: logging.LogRecord = previous(*args, **kwargs)
        record.__class__ = _safe_class(type(record))
        return record

    factory._pch_terminal_safe = True  # type: ignore[attr-defined]
    return factory


def install_log_escaping() -> None:
    """Make every log record terminal-safe, whichever handler emits it. Idempotent.

    The default factory is replaced by :class:`TerminalSafeLogRecord`; a factory
    some host installed is kept and wrapped (see :func:`_escaping_wrapper`). A
    record created before this runs is not covered, which is why the bootstrap
    calls it first.
    """
    current = logging.getLogRecordFactory()
    if current is TerminalSafeLogRecord or getattr(current, "_pch_terminal_safe", False):
        return
    if current is logging.LogRecord:
        logging.setLogRecordFactory(TerminalSafeLogRecord)
    else:
        logging.setLogRecordFactory(_escaping_wrapper(current))


def terminal_safe_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    """``sys.excepthook``: the default traceback print, escaped as human text."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(escape_controls(text))
    sys.stderr.flush()


def install_output_escaping() -> None:
    """Install the process-wide sinks' escaping (logs, uncaught tracebacks). Idempotent.

    Logs: :func:`install_log_escaping`. Tracebacks: ``sys.excepthook`` is
    replaced by :func:`terminal_safe_excepthook` only while it is still the
    interpreter default; a hook some host installed (an IDE, IPython) is left
    alone.
    """
    install_log_escaping()
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = terminal_safe_excepthook
