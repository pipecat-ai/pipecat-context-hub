"""Terminal-safe text: the one output boundary for configured strings.

Contract: no string derived from configuration (env, cwd ``.env``,
``config.toml``, the catalog, paths) reaches a terminal (stdout, stderr or log
output) with a raw control (Cc), format (Cf), line/paragraph separator
(Zl/Zp) or other non-printable character, except the newlines and tabs of the
program's own message layout. Such values are never validated on the way in (a
project ``.env`` can set ``PIPECAT_HUB_CONFIG_FILE`` or the repo lists), so they
can carry terminal escape sequences (OSC 52, CSI), line separators or bidi
controls.

It is enforced at the sinks, not at each call site:

* :func:`echo` is the only ``click.echo`` in ``src/`` (a source test pins it);
  it passes the whole message through :func:`escape_controls`.
* :func:`install_log_escaping` swaps in a log record class whose message and
  formatted traceback go through :func:`escape_controls`, so every handler
  (stderr, ``logging.lastResort``, a test's capture) sees escaped text.
  ``shared/env_loading.load_env_layers`` (the bootstrap every entry point
  calls first) installs it, before any load-time warning.

:func:`shown` is a presentation rule on top, not the safety mechanism: it flags
one configured *value* (a slug or a path) as ``repr() [invalid]`` where a
message names it, so the user sees which value is wrong.

A leaf: stdlib only, nothing from the package (a layering test pins it).
"""

from __future__ import annotations

import logging
from typing import Any

_KEPT = frozenset("\n\t")


def escape_controls(text: str) -> str:
    """``text`` with every non-printable character except ``\\n``/``\\t`` escaped.

    Each such character becomes ``\\xNN``, ``\\uNNNN`` or ``\\UNNNNNNNN``; all
    other text is byte-identical. The output's added characters are printable,
    so applying this twice changes nothing further.
    """
    if text.isprintable():
        return text
    out: list[str] = []
    for ch in text:
        if ch in _KEPT or ch.isprintable():
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
    """``click.echo`` with the whole message passed through :func:`escape_controls`."""
    import click  # deferred: keeps this leaf stdlib-only at import

    click.echo(None if message is None else escape_controls(str(message)), err=err, nl=nl)


class TerminalSafeLogRecord(logging.LogRecord):
    """A record whose message and formatted traceback are escaped for the terminal.

    ``getMessage`` is what every ``Formatter`` renders; ``exc_text`` is where a
    ``Formatter`` caches the traceback it appends (an exception's text can embed
    a configured path). Both stay in ``__dict__`` under their usual names, so
    ``makeLogRecord`` and pickling round-trip unchanged.
    """

    def getMessage(self) -> str:  # noqa: N802 - logging's API name
        return escape_controls(super().getMessage())

    @property
    def exc_text(self) -> str | None:
        value: str | None = self.__dict__.get("exc_text")
        return value

    @exc_text.setter
    def exc_text(self, value: str | None) -> None:
        self.__dict__["exc_text"] = None if value is None else escape_controls(value)


def _escaping_wrapper(previous: Any) -> Any:
    """Wrap a foreign record factory: escape each record's message eagerly."""

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record: logging.LogRecord = previous(*args, **kwargs)
        record.msg = escape_controls(record.getMessage())
        record.args = ()
        return record

    factory._pch_terminal_safe = True  # type: ignore[attr-defined]
    return factory


def install_log_escaping() -> None:
    """Make every log record terminal-safe, whichever handler emits it. Idempotent.

    The default factory is replaced by :class:`TerminalSafeLogRecord`; a factory
    some host installed is kept and wrapped (its records' messages are escaped
    eagerly). A record created before this runs is not covered, which is why the
    bootstrap calls it first.
    """
    current = logging.getLogRecordFactory()
    if current is TerminalSafeLogRecord or getattr(current, "_pch_terminal_safe", False):
        return
    if current is logging.LogRecord:
        logging.setLogRecordFactory(TerminalSafeLogRecord)
    else:
        logging.setLogRecordFactory(_escaping_wrapper(current))
