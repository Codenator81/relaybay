"""Check the built wheel and sdist in dist/: version and the files the server needs at runtime.

uv build && python scripts/check_dist.py [--version 0.1.0]
"""

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "relaybay"
RUNTIME_DIRS = ("web/templates", "web/static", "db/migrations", "policy")


def expected_files() -> set[str]:
    """Templates, static assets, migrations and the policy schema and presets as paths inside
    the package."""
    return {
        path.relative_to(PACKAGE.parent).as_posix()
        for folder in RUNTIME_DIRS
        for path in (PACKAGE / folder).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", help="Expected version (default: any).")
    args = parser.parse_args(argv)
    wheels = sorted(args.dist.glob("relaybay-*.whl"))
    sdists = sorted(args.dist.glob("relaybay-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        print(f"expected one wheel and one sdist in {args.dist}", file=sys.stderr)
        return 1
    wheel, sdist = wheels[0], sdists[0]
    problems = []
    if args.version and not wheel.name.startswith(f"relaybay-{args.version}-"):
        problems.append(f"{wheel.name} is not version {args.version}")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    missing = expected_files() - names
    problems += [f"wheel lacks {name}" for name in sorted(missing)]
    with tarfile.open(sdist) as tf:
        members = {m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name}
    missing = {f"src/{name}" for name in expected_files()} - members
    problems += [f"sdist lacks {name}" for name in sorted(missing)]
    private = [m for m in members if m.split("/")[0] in ("CLAUDE.md", ".claude", ".dev-data")]
    problems += [f"sdist contains {name}" for name in sorted(private)]
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"ok: {wheel.name}, {sdist.name} ({len(expected_files())} runtime files each)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
