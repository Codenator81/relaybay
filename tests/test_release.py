"""Release plumbing: one version, generated reference pages in sync, release notes."""

import importlib.util
import re
import sys
import tomllib
from dataclasses import fields
from pathlib import Path
from types import ModuleType

import pytest

import relaybay
from relaybay.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_version_comes_from_the_package_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert relaybay.__version__ == project["version"]


def test_reference_pages_match_the_code() -> None:
    gen = load_script("gen_reference")
    for path, text in gen.render().items():
        committed = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        assert committed == text, (
            f"{path.name} is out of date: run 'uv run python scripts/gen_reference.py'"
        )


def test_every_mcp_tool_is_explained_in_the_guide() -> None:
    reference = (ROOT / "docs" / "reference" / "mcp.md").read_text(encoding="utf-8")
    tools = re.findall(r"^### `(\w+)`$", reference, flags=re.M)
    assert len(tools) >= 15
    guide = (ROOT / "docs" / "mcp-tools.md").read_text(encoding="utf-8")
    missing = [t for t in tools if f"`{t}" not in guide]
    assert not missing, f"docs/mcp-tools.md does not mention: {missing}"


def test_environment_variables_follow_the_setting_names() -> None:
    for f in fields(Settings):
        env = f.metadata.get("env")
        if env:
            assert env == f"RELAYBAY_{f.name.upper()}"


CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2027-01-01

### Added

- Something new.

## [0.1.0] - 2026-11-02

First release.

### Added

- The board.

[Unreleased]: https://github.com/example/compare/v0.2.0...HEAD
[0.1.0]: https://github.com/example/releases/tag/v0.1.0
"""


def test_release_notes_are_one_version_section() -> None:
    notes = load_script("changelog_notes").notes
    assert notes(CHANGELOG, "0.1.0") == "First release.\n\n### Added\n\n- The board."
    assert notes(CHANGELOG, "0.2.0") == "### Added\n\n- Something new."
    assert notes(CHANGELOG, "Unreleased") is None
    assert notes(CHANGELOG, "0.0.9") is None


def test_the_current_version_has_release_notes() -> None:
    if ".dev" in relaybay.__version__:
        pytest.skip("unreleased development version")
    notes = load_script("changelog_notes").notes
    assert notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), relaybay.__version__)
