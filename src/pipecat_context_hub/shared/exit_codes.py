"""Process exit codes shared by `serve`, `refresh` and the one-shot query commands."""

from __future__ import annotations

# Invalid input: a bad argument, or a refused request (`repos add` of a default repo,
# a shadowed layer, an untrusted config file, ...). Nothing was changed.
EXIT_INVALID_INPUT = 1
# The local index is missing, empty, unreadable or incompatible (or, for
# `refresh`, a fatal index error stopped it).
EXIT_INDEX_UNREADY = 2
# sysexits EX_TEMPFAIL: the refresh lock is busy (a `refresh` is rewriting the
# index); retry shortly.
EXIT_REFRESH_IN_PROGRESS = 75
