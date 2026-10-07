"""Prepare a portable skills-only plugin copy without installing its runtime."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path


def prepare_local(destination: Path) -> Path:
    """Copy only skills-only package resources into a fresh destination."""
    source = Path(__file__).resolve().parents[1]
    checkout = next((p for p in source.parents if (p / ".git").exists()), source)
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ValueError("Destination must not be a symlink")
    destination = destination.resolve()
    if destination == checkout or checkout in destination.parents:
        raise ValueError("Destination must be outside the checkout")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be absent or an empty directory")

    files = [source / name for name in ("plugin.json", "README.md", "PRIVACY.md", "LICENSE")]
    files.append(source / "scripts" / "prepare_local.py")
    files.extend(
        source / "assets" / name
        for name in (
            "cat-mark.svg",
            "cat-mark-dark.svg",
            "context-hub-logo.svg",
            "context-hub-logo-dark.svg",
        )
    )
    files.extend(
        source / "skills" / name / "SKILL.md" for name in ("build", "deploy", "explore", "setup")
    )
    for path in files:
        if not path.is_file() or any(
            part.is_symlink() for part in (path, *path.parents) if part != source.parent
        ):
            raise ValueError(f"Package resource must be a regular file: {path.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hub-plugin-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "package"
        staged.mkdir()
        for path in files:
            target = staged / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        if destination.exists():
            destination.rmdir()  # Refuses if anything appeared since preflight.
        staged.rename(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        destination = prepare_local(args.destination)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(
        f"Prepared skills-only plugin at {destination}; no runtime installation or index refresh performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
