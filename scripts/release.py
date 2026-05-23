#!/usr/bin/env python3
"""Cut a new offwork release.

Usage::

    python scripts/release.py {major|minor|patch}      # bump from current
    python scripts/release.py 1.2.3                    # set explicit version
    python scripts/release.py {major|minor|patch} --dry-run
    python scripts/release.py {major|minor|patch} --skip-tests
    python scripts/release.py {major|minor|patch} --skip-publish

Steps performed:

1. Refuse to run on a dirty working tree (override with ``--allow-dirty``).
2. Bump the version in ``pyproject.toml`` (single source of truth).
3. Run ``mypy offwork`` and ``pytest -q`` (skip with ``--skip-tests``).
4. Build sdist + wheel into ``dist/`` and run ``twine check``.
5. Create the git commit + annotated tag ``vX.Y.Z``.
6. Push commit and tag to ``origin`` (skip with ``--skip-push``).
7. Upload to PyPI via ``twine`` (skip with ``--skip-publish``).

The script is idempotent up to step 5: anything before the tag can be
re-run safely after fixing problems.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DIST = _REPO_ROOT / "dist"

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


# -- Helpers ----------------------------------------------------------------


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"\x1b[36m$ {' '.join(cmd)}\x1b[0m", flush=True)
    return subprocess.run(cmd, cwd=_REPO_ROOT, check=check, text=True)


def _capture(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, cwd=_REPO_ROOT, check=True, text=True, capture_output=True,
    ).stdout.strip()


def _current_version() -> str:
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]  # type: ignore[no-any-return]


def _bump(current: str, part: str) -> str:
    m = _SEMVER_RE.match(current)
    if not m:
        raise SystemExit(f"Cannot parse current version {current!r}")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"Unknown bump part: {part}")


def _resolve_target(arg: str, current: str) -> str:
    if arg in ("major", "minor", "patch"):
        return _bump(current, arg)
    if _SEMVER_RE.match(arg):
        return arg
    raise SystemExit(
        f"Argument must be one of 'major', 'minor', 'patch', or X.Y.Z — got {arg!r}"
    )


def _write_version(new_version: str) -> None:
    text = _PYPROJECT.read_text()
    updated, n = re.subn(
        r'^version = "[^"]+"$',
        f'version = "{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise SystemExit("Could not locate the 'version = ...' line in pyproject.toml")
    _PYPROJECT.write_text(updated)


def _git_clean() -> bool:
    return _capture(["git", "status", "--porcelain"]) == ""


def _tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        cwd=_REPO_ROOT, capture_output=True,
    )
    return result.returncode == 0


# -- Main flow --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="'major' | 'minor' | 'patch' | X.Y.Z")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without changing anything")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Skip the clean-working-tree check")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Skip mypy and pytest")
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip building the sdist/wheel and twine check")
    parser.add_argument("--skip-tag", action="store_true",
                        help="Skip the git commit + tag")
    parser.add_argument("--skip-push", action="store_true",
                        help="Skip pushing the commit and tag to origin")
    parser.add_argument("--skip-publish", action="store_true",
                        help="Skip uploading to PyPI")
    parser.add_argument("--repository", default="pypi",
                        help="twine --repository value (default: pypi)")
    args = parser.parse_args()

    current = _current_version()
    target = _resolve_target(args.target, current)
    tag = f"v{target}"

    print(f"\x1b[1mRelease plan:\x1b[0m {current} → \x1b[32m{target}\x1b[0m  (tag: {tag})")

    if not args.allow_dirty and not _git_clean():
        raise SystemExit(
            "Working tree is dirty. Commit/stash changes or pass --allow-dirty."
        )

    if _tag_exists(tag) and not args.skip_tag:
        raise SystemExit(f"Tag {tag} already exists. Bump again or pass --skip-tag.")

    if args.dry_run:
        print("\n--dry-run: stopping before any side effects.")
        return 0

    # 1. Bump version in pyproject.toml
    _write_version(target)
    print(f"Wrote version {target} to pyproject.toml")

    # 2. Sanity checks
    if not args.skip_tests:
        _run([sys.executable, "-m", "mypy", "offwork"])
        _run([sys.executable, "-m", "pytest", "-q", "--ignore=tests/test_e2e.py"])

    # 3. Build
    if not args.skip_build:
        if _DIST.exists():
            shutil.rmtree(_DIST)
        _run([sys.executable, "-m", "pip", "install", "--quiet",
              "--upgrade", "build", "twine"])
        _run([sys.executable, "-m", "build"])
        _run([sys.executable, "-m", "twine", "check", "dist/*"])

    # 4. Commit + tag
    if not args.skip_tag:
        _run(["git", "add", "pyproject.toml"])
        _run(["git", "commit", "-m", f"Release {target}"])
        _run(["git", "tag", "-a", tag, "-m", f"Release {target}"])

    # 5. Push
    if not args.skip_push and not args.skip_tag:
        _run(["git", "push", "origin", "HEAD"])
        _run(["git", "push", "origin", tag])

    # 6. Publish to PyPI
    if not args.skip_publish and not args.skip_build:
        _run([
            sys.executable, "-m", "twine", "upload",
            "--repository", args.repository, "dist/*",
        ])

    print(f"\n\x1b[32m✓ Released {target}\x1b[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
