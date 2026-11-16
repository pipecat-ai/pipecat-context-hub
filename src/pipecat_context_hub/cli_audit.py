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
    echo(
        f"Share bundle (review before sending): {result.share}\n"
        f"Tarball: {result.tarball} (sha256 {result.sha256[:16]}...)\n"
        "Nothing has been sent. Next: read share/report.md, then `audit gist`, send the tarball "
        "yourself, or `audit discard`.",
        err=True,
    )
    import json

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
def gist_cmd(out_dir: Path, yes: bool) -> None:
    """Post OUT_DIR/share/ as a SECRET GitHub gist (unlisted, NOT private).

    Anyone with the link can read it. A gist holds text only, so it carries the five share/
    files, not the tarball. Without --yes this only lists what would be posted.
    """
    from pipecat_context_hub.audit.extract import BUNDLE_FILES

    share = out_dir / "share"
    files = [share / n for n in BUNDLE_FILES]
    missing = [f.name for f in files if not f.is_file() or f.is_symlink()]
    if missing:
        _fail(f"{share.name}/ is missing {missing}; run `audit extract` first")
    echo("Would post as a SECRET gist (unlisted, not private: anyone with the link can read it):")
    for f in files:
        echo(f"  {f.name}  {f.stat().st_size} bytes")
    if not yes:
        echo("Preview only. Re-run with --yes to create the gist.", err=True)
        return
    gh = shutil.which("gh")
    if gh is None:
        _fail("`gh` not found; send share.tar.gz yourself instead")
    argv = [gh, "gist", "create", *map(str, files), "--desc", "Context Hub session bundle"]
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell, secret gist (no --public)
            argv, capture_output=True, text=True, timeout=_GIST_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
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

    Bundles are untrusted input: tarballs are read in memory, only the five known filenames are
    accepted, and bundles with a wrong schema version or malformed rows are rejected.
    """
    import json

    from pipecat_context_hub.audit.ingest import merge_bundles

    try:
        result = merge_bundles(list(bundles), out)
    except OSError as exc:
        _fail(f"cannot write {out.name}: {type(exc).__name__}")
    for row in result["report"]:
        if row["status"] != "ok":
            echo(f"REJECTED {row['source']}: {row['reason']}", err=True)
    summary = {k: v for k, v in result.items() if k != "report"}
    echo_json(json.dumps(summary))
    if result["bundles_ok"] == 0:
        raise SystemExit(EXIT_INVALID_INPUT)


def register_audit_commands(group: click.Group) -> None:
    """Attach the ``audit`` command group to the main CLI group."""
    group.add_command(audit_group)
