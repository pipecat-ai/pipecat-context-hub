"""The ``audit`` command group: review how coding agents used Context Hub in real sessions.

Participant side (offline, nothing is sent unless you pass ``--yes``)::

    pipecat-context-hub audit extract -i ~/.claude/projects/<project> --out ./hub-audit --since 2026-11-18
    # review ./hub-audit/share/report.md, then choose one:
    pipecat-context-hub audit gist ./hub-audit --yes   # secret (unlisted) GitHub gist
    # ...or send ./hub-audit/share.tar.gz yourself, or:
    pipecat-context-hub audit discard ./hub-audit --yes

Organizer side: ``audit ingest`` validates and merges bundles received from participants.

Every line goes through ``shared/terminal.echo`` / ``echo_json``: queries, error text and archive
member names come from other people's transcripts and bundles.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - runs `gh gist create` with a fixed argv, never a shell
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import click

from pipecat_context_hub.shared.exit_codes import EXIT_INVALID_INPUT
from pipecat_context_hub.shared.terminal import echo, echo_json

_GIST_TIMEOUT_S = 60


def _fail(message: str) -> NoReturn:
    echo(f"Error: {message}", err=True)
    raise SystemExit(EXIT_INVALID_INPUT)


def _since(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise click.BadParameter("expected YYYY-MM-DD") from None
    return value


@click.group("audit")
def audit_group() -> None:
    """Audit coding-agent sessions for Context Hub usage (Claude Code, Codex, pi).

    \b
    extract   read transcripts, write full local output + a scrubbed share/ bundle and tarball
    repack    rebuild share.tar.gz after you edited share/
    gist      post share/ as a SECRET (unlisted, not private) GitHub gist; needs --yes
    discard   delete what `extract` wrote; needs --yes
    ingest    organizers: validate and merge bundles received from participants
    """


@audit_group.command("extract")
@click.option(
    "-i",
    "--input",
    "inputs",
    multiple=True,
    required=True,
    type=click.Path(path_type=Path),
    help="Transcript directory (repeatable). One sub-directory per participant when collecting.",
)
@click.option("--out", required=True, type=click.Path(path_type=Path), help="Output directory.")
@click.option("--anonymize", is_flag=True, help="Replace participant labels with participant-N.")
@click.option(
    "--since",
    callback=_since,
    default=None,
    help="YYYY-MM-DD: skip sessions that started earlier (limit to the event).",
)
def extract_cmd(inputs: tuple[Path, ...], out: Path, anonymize: bool, since: str | None) -> None:
    """Extract Context Hub usage into OUT. Nothing is sent anywhere."""
    from pipecat_context_hub.audit.extract import run_extract

    try:
        result = run_extract(list(inputs), out, anonymize=anonymize, since=since)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
    for warning in result.warnings:
        echo(f"warning: {warning}", err=True)
    from pipecat_context_hub.audit.bundle import has_content

    counts = result.counts
    if not has_content(counts):
        echo(
            f"No Context Hub calls found in {counts['sessions']} sessions, so there is nothing to "
            f"share: no Context Hub calls and no Pipecat work without it. Local detail is in "
            f"{out}; `audit discard {out} --yes` removes it. Nothing has been sent.",
            err=True,
        )
    else:
        if counts["call_rows"] == 0 and counts["missed_opportunity"]:
            echo(
                f"No Context Hub calls, but {counts['missed_opportunity']} session(s) did Pipecat "
                "work without it: the bundle explains why (see 'Answers to the four questions' in "
                "share/report.md), and that is worth sending.",
                err=True,
            )
        elif counts["call_rows"] == 0:
            echo(
                f"Context Hub use was found in {counts['hub_used']} session(s) only by a text scan "
                "(a harness without an adapter): the bundle records it without per-call rows.",
                err=True,
            )
        echo(
            f"Share bundle (review before sending): {result.share}\n"
            f"Tarball: {result.tarball} (sha256 {result.sha256[:16]}...)\n"
            "Nothing has been sent. Next: read share/report.md, then `audit gist`, send the "
            "tarball yourself, or `audit discard`.",
            err=True,
        )
    echo_json(json.dumps(result.summary, indent=2))


@audit_group.command("repack")
@click.argument("out_dir", type=click.Path(path_type=Path))
def repack_cmd(out_dir: Path) -> None:
    """Rebuild OUT_DIR/share.tar.gz from an edited OUT_DIR/share/."""
    from pipecat_context_hub.audit.extract import repack

    try:
        tar_path, digest = repack(out_dir)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
    echo(f"{tar_path} sha256 {digest}")


@audit_group.command("gist")
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option("--yes", is_flag=True, help="Actually create the gist (default: preview only).")
@click.option(
    "--allow-empty",
    is_flag=True,
    help="Post even when the bundle has no Context Hub calls and no Pipecat work without it.",
)
def gist_cmd(out_dir: Path, yes: bool, allow_empty: bool) -> None:
    """Post OUT_DIR/share/ as a SECRET GitHub gist (unlisted, NOT private).

    Anyone with the link can read it. A gist holds text only, so it carries the five share/
    files, not the tarball. Without --yes this only lists what would be posted.
    """
    from pipecat_context_hub.audit.bundle import (
        BUNDLE_FILES,
        BundleError,
        has_content,
        load_files,
        omitted_when_posting,
    )
    from pipecat_context_hub.audit.egress import verify_share

    share = out_dir / "share"
    files = [share / n for n in BUNDLE_FILES]
    missing = [f.name for f in files if not f.is_file() or f.is_symlink()]
    if missing:
        _fail(f"{share.name}/ is missing {missing}; run `audit extract` first")
    leaks = verify_share(share, BUNDLE_FILES)
    if leaks:
        _fail(
            f"share/ failed the leak check ({'; '.join(leaks)}): remove what it names, then run "
            "`audit repack`"
        )
    data = {f.name: f.read_bytes() for f in files}
    try:
        bundle = load_files(data)  # the validator `ingest` runs: what is posted, ingests
    except BundleError as exc:
        _fail(f"share/ is not a valid bundle ({exc}); fix it, then run `audit repack`")
    if not allow_empty and not has_content(bundle.manifest["counts"]):
        _fail(
            "this bundle has no Context Hub calls and no Pipecat work without it, so a gist would "
            "carry nothing useful (only session counters and timestamps); pass --allow-empty to "
            "post it anyway"
        )
    # A gist cannot hold an empty file: a JSONL file with zero rows is left out, and `audit ingest`
    # accepts the bundle without it because the manifest says that file has zero rows.
    empty = [f for f in files if omitted_when_posting(f.name, data[f.name])]
    files = [f for f in files if f not in empty]
    echo("Would post as a SECRET gist (unlisted, not private: anyone with the link can read it):")
    for f in files:
        echo(f"  {f.name}  {len(data[f.name])} bytes")
    for f in empty:
        echo(f"  ({f.name} has no rows and is left out: a gist cannot hold an empty file)")
    if not yes:
        echo("Preview only. Re-run with --yes to create the gist.", err=True)
        return
    gh = shutil.which("gh")
    if gh is None:
        _fail("`gh` not found; send share.tar.gz yourself instead")
    # Absolute paths after `--`: an out dir named like a flag (`./-pd`, which pathlib shortens to
    # `-pd/...`) must never be read by gh as `-p` (--public) plus `-d`.
    paths = [str(f.resolve()) for f in files]
    argv = [gh, "gist", "create", "--desc", "Context Hub session bundle", "--", *paths]
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell, secret gist (no --public)
            argv, capture_output=True, text=True, timeout=_GIST_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        _fail(
            f"gh gist create timed out after {_GIST_TIMEOUT_S}s and may still have created the gist: "
            "check `gh gist list` before retrying"
        )
    except OSError as exc:
        _fail(f"gh gist create failed: {type(exc).__name__}")
    if proc.returncode != 0:
        _fail(f"gh gist create exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    echo(f"Gist created (secret, unlisted): {proc.stdout.strip()}")


@audit_group.command("discard")
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option("--yes", is_flag=True, help="Actually delete (default: list what would go).")
def discard_cmd(out_dir: Path, yes: bool) -> None:
    """Delete exactly the files `audit extract` wrote into OUT_DIR (never your transcripts)."""
    from pipecat_context_hub.audit.extract import generated_paths

    try:
        paths = generated_paths(out_dir)
    except ValueError as exc:
        _fail(str(exc))
    for p in paths:
        echo(f"  {p}")
    if not yes:
        echo("Preview only. Re-run with --yes to delete these files.", err=True)
        return
    for p in paths:
        p.unlink()
    for d in (out_dir / "share", out_dir):
        try:
            d.rmdir()  # only if now empty: anything else of yours stays
        except OSError:
            pass
    echo(f"Deleted {len(paths)} files.")


@audit_group.command("ingest")
@click.argument("bundles", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path), help="Merged output dir.")
def ingest_cmd(bundles: tuple[Path, ...], out: Path) -> None:
    """Validate and merge bundles (share.tar.gz, share/ dirs or cloned gists) into OUT.

    Bundles are untrusted input: tarballs are read in memory as a stream with fixed size and member
    limits, only the five known filenames are accepted, and bundles with a wrong schema version,
    malformed rows or counts that disagree with the rows are rejected. Rows are namespaced by their
    source bundle, so one bundle cannot shadow another's.
    """
    from pipecat_context_hub.audit.ingest import merge_bundles

    try:
        result = merge_bundles(list(bundles), out)
    except OSError as exc:
        _fail(f"cannot write {out.name}: {type(exc).__name__}")
    for row in result["report"]:
        if row["status"] != "ok":
            echo(f"REJECTED {row['source']}: {row['reason']}", err=True)
        elif row.get("ref_collisions") or row.get("session_collisions"):
            echo(
                f"WARNING {row['source']}: {row['ref_collisions']} call ref(s) and "
                f"{row['session_collisions']} session(s) also appear in another bundle; both are "
                "kept, each row names its `source_bundle` (see ingest.json)",
                err=True,
            )
    summary = {k: v for k, v in result.items() if k != "report"}
    echo_json(json.dumps(summary))
    if result["bundles_ok"] == 0:
        raise SystemExit(EXIT_INVALID_INPUT)


def register_audit_commands(group: click.Group) -> None:
    """Attach the ``audit`` command group to the main CLI group."""
    group.add_command(audit_group)
