"""Frozen builds and installers: what can be checked without building them."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker

from relaybay.desktop import single_instance

ROOT = Path(__file__).resolve().parent.parent
# The platforms the installers are built for (desktop.yml).
TARGETS = {
    "Windows": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
    "macOS": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
    "Linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
}
HEAVY = ("fastapi", "starlette", "uvicorn", "pydantic", "mcp", "jinja2", "aiosqlite", "webview")


def bundled_packages() -> set[str]:
    """relaybay's runtime closure with the `desktop` extra on any target, from uv.lock."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    root = packages["relaybay"]
    bundled: set[str] = set()
    for env in TARGETS.values():
        environment = {"python_version": "3.12", "python_full_version": "3.12.0", **env}
        found: set[str] = set()
        todo = [*root["dependencies"], *root["optional-dependencies"]["desktop"]]
        while todo:
            dep = todo.pop()
            marker = dep.get("marker")
            if marker and not Marker(marker).evaluate({**environment, "extra": ""}):
                continue
            package = packages[dep["name"]]
            todo.extend(
                d
                for extra in dep.get("extra", [])
                for d in package.get("optional-dependencies", {}).get(extra, [])
            )
            if dep["name"] in found:
                continue
            found.add(dep["name"])
            todo.extend(package.get("dependencies", []))
        bundled |= found
    return bundled


def test_every_bundled_package_has_its_licence_listed() -> None:
    text = (ROOT / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    listed = set(re.findall(r"^([a-z0-9][a-z0-9-]*) {2,}\S", text, flags=re.M))
    bundled = bundled_packages()
    assert "pystray" in bundled
    assert "pyobjc-core" in bundled  # the macOS closure is walked too
    assert not bundled - listed, f"add to THIRD_PARTY_LICENSES.txt: {sorted(bundled - listed)}"
    assert not listed - bundled, f"no longer bundled: {sorted(listed - bundled)}"


def test_hook_forwarder_imports_only_the_standard_library() -> None:
    code = "import json, sys; import relaybay.hook_client; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout
    loaded = {name.split(".")[0] for name in json.loads(out)}
    assert not loaded.intersection(HEAVY), sorted(loaded.intersection(HEAVY))


def test_installer_waits_for_the_program_mutex() -> None:
    script = (ROOT / "packaging" / "windows" / "relaybay.iss").read_text(encoding="utf-8")
    assert f'#define AppMutexName "{single_instance.APP_MUTEX}"' in script
