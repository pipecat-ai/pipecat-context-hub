# Command-Line Guide

Every MCP tool is also a one-shot CLI command. Queries use the same handlers as
MCP, print JSON to stdout and send logs and warnings to stderr.

## Query commands

| MCP tool | CLI command |
|---|---|
| `search_docs` | `search-docs` |
| `get_doc` | `get-doc` |
| `search_examples` | `search-examples` |
| `get_example` | `get-example` |
| `search_api` | `search-api` |
| `get_code_snippet` | `get-code-snippet` |
| `check_deprecation` | `check-deprecation` |
| `get_hub_status` | `status` |

```bash
uvx pipecat-ai-context-hub status
uvx pipecat-ai-context-hub search-api "DailyTransport" --limit 3
uvx pipecat-ai-context-hub search-docs "TTS + STT"
uvx pipecat-ai-context-hub get-doc --path /guides/learn/transports
uvx pipecat-ai-context-hub check-deprecation PipelineTask --at-version 2.0.0
```

Use `<command> --help` for its options. See [MCP tools](tools.md) for filters,
multi-concept queries and response fields, and [version-aware queries](version-aware-queries.md)
for compatibility scoring. `status` and `check-deprecation` do not load models;
semantic queries do.

`refresh` reads Pipecat's machine-readable `scripts/deprecations/deprecations.json`
(and, when present, `scripts/deprecations/removals.json`) from the indexed
framework checkout. Deprecation checks do not require GitHub CLI authentication.

`check-deprecation` bare-name rule: A name that matches no registry symbol exactly but names deprecated members returns no verdict and lists them; query one of the listed symbols.
Example: `check-deprecation trigger_user_turn_stopped` prints `deprecated: false`, `status: null`, a `note` and one entry in `candidates` (`BaseUserTurnStopStrategy.trigger_user_turn_stopped`, replacement `report_user_turn_stopped`). `status: null` with `candidates` is that hint; `status: null` with only a `note` means the deprecation map could not be loaded. Every response carries `candidates` and `candidates_total` (`null` when absent); at most 10 candidates are listed and `candidates_total` is the full count. An exact bare key such as `InputParams` keeps its verdict (`deprecated: true`) and attaches the other same-named members as candidates. A dotted name that matches neither an exact key nor a prefix of one (a lower-case owner such as `foo.bar`, or a multi-home name) stays `current`.

## Audit commands (session feedback)

`audit` reviews how coding agents used Context Hub in real sessions. It reads local transcripts
(Claude Code, Codex, pi; other formats are only text-scanned), works offline, and **sends nothing
unless you run `gist --yes`**.

```bash
pipecat-context-hub audit extract -i ~/.claude/projects/<project> --out ./hub-audit --since 2026-11-18   # --anonymize: participant-N labels
# read ./hub-audit/share/report.md (what would be shared), then choose one:
pipecat-context-hub audit gist ./hub-audit --yes      # secret gist: unlisted, NOT private (refuses a bundle with nothing to share unless --allow-empty)
#   ...or send ./hub-audit/share.tar.gz yourself (email, chat), or:
pipecat-context-hub audit discard ./hub-audit --yes   # deletes only files `extract` wrote
pipecat-context-hub audit repack ./hub-audit          # after editing share/ by hand: re-validates rows, re-counts, regenerates report.md, re-checks for leaks
pipecat-context-hub audit ingest a.tar.gz b.tar.gz --out ./merged   # organizers
```

Participant labels: with one sub-directory per person under an input (`collected/<person>/...`),
each sub-directory is a participant, and a file directly under the input (a flat root) gets the
input's own name. Two layouts are one participant: a Claude Code project's
`<session-id>/subagents/` files (labelled with the input's name), and Codex's `YYYY/MM/DD/` date
directories, whether `-i` is `~/.codex/sessions` or a year, month or day under it (labelled with
the name of the input's first ancestor that is not a date component, `sessions`).

The `share/` bundle carries tool names, queries, result shapes (hit counts, confidence), flags and
latency, with opaque ids; transcript text, shell commands and paths are dropped or replaced. A shell
call is shared only as the hub command it ran, rebuilt from its parsed argv and re-quoted (inert data:
organizers rebuild a call from it by hand and never execute it); a line using a construct the parser
does not model (`$'...'`, `${...}`, `$((...))`, a pipe or here-string into a shell, ...) is still
counted but shares no command text. A Codex JS batch call counts only in code (not in a string,
comment or regex; a comment holding a line break is a line break) and shares only its literal
arguments, and only when it surely runs: at the top level of the batch, outside parentheses,
brackets and braces, not in a control construct's body (`if`, a loop, `switch`, `try`, a function,
`?`, `&&`, `||`, `??`), not in a labelled statement, and not after an earlier top-level `throw`,
`return`, `break`, `continue` or `process.exit`; the elements of `Promise.all([...])` /
`Promise.allSettled([...])` count. A field whose name contains a credential word (`api_key`,
`password1`, `OPENAI_API_KEY_2`...) is replaced whole, and so is the `value` of a name/value pair
(`{"name": "API_KEY", "value": ...}`, a Kubernetes `env` entry), and a labelled value in free text
(`password: ...`, `API key = ...`, `--api-key ...`, `Authorization: Basic ...`, URL userinfo, an
escaped `\"token\": \"...\"`); a quoted value goes whole, an unquoted value with no space goes
whole whatever punctuation it holds (`&` ends it only in a URL query, `,` `;` `)` `]` `}` only
inside brackets opened before the label), and there is no minimum length. Plural labels count
(`API_TOKENS`, `SSH_KEYS`); `max_tokens`, `token_count`, `password_hint`, `password_file` do not. A
placeholder is kept only in a fixed shape (`$TOKEN`, `${API_KEY}`, `<token>`, `YOUR_API_KEY`,
`...`, `xxx`) that holds no token shape, so `${AKIA...}` is redacted; a `YOUR_` / `your-`
placeholder's words must name a credential (`your-api-key` is kept, `your-dog-rex` is a value). A
number of 32+ digits is replaced too.

The label grammar, one form per row of `egress.LABEL_FORMS` (the tests are generated from it): a
separator (`password: v`, `password = v`, `password => v`, `api_key → v`, `password⇒v`); a two-word
label (`API key: v`); a quoted value, single, double, triple or escaped (`"password": "v"`,
`password = """v"""`, `\"token\": \"v\"`); a credential flag (`--api-key v`, `--password=v`,
`--token "v"`); a markdown label (`**password**: v`, `` `password`: v ``, `[password]: v`);
subscript or attribute assignment (`os.environ["DAILY_API_KEY"] = "v"`, `config.password = v`);
unicode spellings (curly quotes, NBSP, zero-width characters, which are dropped before label
detection, and fullwidth `：` `＝`, read as `:` `=`); a value on the next line (`password:` then an
indented `v`, or a YAML `|` / `>` block: every following line indented deeper than the label's line
goes too, up to a dedent); a URL query (`?a=b&password=v`, the value running to the next `&`); an
`Authorization` scheme (`Basic v`, `Bearer v`, any run of blanks after the scheme word); a `Cookie`
/ `Set-Cookie` header (the whole value, blank runs included); netrc (`machine h login bob password
v`); a user flag (`curl -u alice:v`, `--user alice:v`); a glued MySQL password (`mysql -pv`); URL
userinfo (`postgres://admin:v@db`: the whole `scheme://user:password@` span, up to the last `@`
before a blank or quote, is replaced by `<redacted>`, so the scheme and user are not kept); a
name/value pair (`{"name": "API_KEY", "value": "v"}`, `- name: API_KEY` then `value: v`, `<add
key="ApiKey" value="v"/>`); an XML element (`<password>v</password>`); SQL (`IDENTIFIED BY 'v'`,
`IDENTIFIED BY v`, `PASSWORD 'v'`); prose `password is v`; `set password v`, `export PGPASSWORD v`,
`setenv TOKEN v`; and `sshpass -p v`. A segment counts as a credential label when it names one
(`secret`, `token`, `apikey`, `password`, `passwd`, `pwd`, `credential`, `cookie`, ... and the whole
words `pass`, `pw`, `auth`, so `SMTP_PASS` and `DB_AUTH` count), also run together with other
words (`tokenizerkey`); a whole non-credential word (`tokenizer`, `secretary`, `bypass`, `author`)
does not.

The cost is over-redaction, accepted on purpose: `password reset` alone on a line reads as netrc,
a 1-5 digit URL password followed by `/` reads as a port and is kept, `auth: yes` and `pass: 3` are
replaced, a cookie header loses its pair names too, an `Authorization` or cookie value runs past two
blanks to the end of its line, `token is invalid` loses `invalid`, `set token mode` loses `mode`,
every deeper-indented line under a label with an empty value goes, and a `{"key": ..., "value":
...}` object loses both values (`key` alone is a credential label).

**Limit.** Redaction is best-effort over free text: forms outside the grammar above are not
detected (an unlabelled secret, a label in another language, a value split across fields, a
credential passed to a call (`set_password("v")`), prose without `is` (`the token v`), Terraform's
`variable "password" { default = "v" }`, `IDENTIFIED WITH plugin BY 'v'`, an HTML-escaped tag), and
a `\uXXXX`-escaped JSON string nested inside another string is not decoded. Queries are your own
words, so read `report.md` before sharing.

Before anything is kept or posted, `extract`, `repack` and `gist` run a leak check over the
finished files: JSON is checked decoded (every string, key and repeated key, so a `\u0073k-...`
escape cannot hide a token), and a hit refuses the whole bundle, naming the file and rule but never
the matched text; `report.md` is checked one table cell (or other line) at a time, so no match joins
two rendered values, and `SCHEMA.md` is checked whole (`report.md` joins call/count pairs with ` ↦ `,
because `→` is a label separator). "Nothing to share" means no call rows, no session that used Context Hub and no
session that did Pipecat work without it; `gist` refuses such a bundle unless `--allow-empty`, and
leaves out a `calls.jsonl` or `sessions.jsonl` with no rows (a file holding only whitespace counts
as empty; a gist cannot hold an empty file). `gist`, `repack` and `ingest` read a share/ directory
the same way (no symlinked directory or file, size limits) and `extract`, `repack`, `gist` and
`ingest` validate a bundle with the same rules, including the shape of `manifest.json`'s `analysis`
and each call's `latency_s` and `input`, so a bundle `repack` accepts is one `ingest` accepts; after
a hand edit, run `repack` before `gist` (`gist` refuses a `report.md` the rows no longer render).
`extract` refuses an `--out` that is, contains or sits inside an input directory, or that holds
files it did not write. A refused `extract` or `repack` writes nothing: no file is
kept or rewritten until the whole bundle has passed, and no output is ever written through a
symlink. `gist` posts the bytes it checked, and refuses a `gh` found in the current directory or
reached through a relative `PATH` entry (`node_modules/.bin`).

`ingest` treats bundles as untrusted: tarballs are read in memory as a stream with fixed limits
(archive size, member count, member size, total bytes), only the five known filenames are accepted
once each, and rows are validated. Merged rows are namespaced by their source bundle, so one bundle
cannot shadow another's; the same bundle given twice merges once, and two bundles carrying the same
ref or session are both kept and reported in `ingest.json`. The `context-hub-session-audit` skill
(`.claude/skills/`) adds the organizer's judgment rubric.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Invalid input |
| `2` | Index missing, empty, unreadable or incompatible |
| `75` | A refresh is rewriting the index; retry in 60–120 seconds |

These are query exit codes. See [troubleshooting](troubleshooting.md) for refresh
failures, index recovery and lock contention.

## Inside the Pipecat CLI

Installing this package alongside `pipecat-ai[cli]` mounts it as `pipecat context-hub`,
with `pipecat ch` as a shorter alias (hidden from `pipecat --help`, but equivalent).
Discovery is dynamic, so this works with a Pipecat CLI that is already
installed — no upgrade needed on that side.

```bash
pipecat context-hub --help                          # same Context Hub commands
pipecat context-hub refresh
pipecat context-hub check-deprecation PipelineTask
```

`pipecat context-hub <command>` and `pipecat-context-hub <command>` are equivalent —
same parsing, same JSON, same exit codes. Point MCP clients at this package
directly rather than at `pipecat context-hub serve`, though, so starting the server does
not load the Pipecat CLI first. `pipecat context-hub install` works out the right command
for your setup — note that a `--with` co-install exposes only the Pipecat CLI's
own scripts, so `pipecat-context-hub` is importable but not on `PATH`.
