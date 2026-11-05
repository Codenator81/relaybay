# PyInstaller spec: one onedir folder with three executables.
#
#   uv sync --locked --extra desktop --group build
#   uv run pyinstaller --noconfirm packaging/relaybay.spec
#
# Output: dist/relaybay/ (Windows, Linux) with relaybay, relaybay-desktop and relaybay-hook next
# to a shared _internal/ folder; on macOS also dist/relaybay.app (the same three in Contents/MacOS).
# Onedir, never onefile: pystray (LGPL-3.0) stays a separate, replaceable set of module files.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
LAUNCHERS = ROOT / "packaging" / "launchers"
ICONS = ROOT / "packaging" / "icons"
VERSION = __import__("relaybay").__version__

if VERSION == "0.0.0":
    raise SystemExit("relaybay is not installed in this environment: run `uv sync` first")

# Templates, static files, migrations, the policy schema and presets, the desktop form and icons,
# plus the package metadata: without it importlib.metadata finds nothing and __version__ is 0.0.0.
datas = [
    *collect_data_files("relaybay"),
    *copy_metadata("relaybay"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_LICENSES.txt"), "."),
]

# Never part of the program: PyInstaller's own setuptools / pkg_resources hooks would otherwise
# collect setuptools' vendored packages, which THIRD_PARTY_LICENSES.txt does not list.
BUILD_ONLY = ["pkg_resources", "setuptools", "tkinter"]

# Everything the hook forwarder must never pull in: its import chain is the standard library plus
# relaybay.domain (hooks, sessions, stages, models, errors), so it starts in tens of milliseconds.
HOOK_EXCLUDES = [
    "aiosqlite",
    "anyio",
    "clr",
    "fastapi",
    "gi",
    "jinja2",
    "jsonschema",
    "markdown_it",
    "mcp",
    "objc",
    "PIL",
    "platformdirs",
    "pydantic",
    "pydantic_core",
    "pystray",
    "pythonnet",
    "regex",
    "snowballstemmer",
    "sqlite3",
    "starlette",
    "uvicorn",
    "webview",
]


def analysis(script, excludes=(), datas=()):
    return Analysis(
        [str(LAUNCHERS / script)],
        pathex=[str(ROOT / "src")],
        datas=list(datas),
        excludes=[*BUILD_ONLY, *excludes],
        noarchive=False,
    )


main_a = analysis("relaybay_cli.py", datas=datas)
desktop_a = analysis("relaybay_desktop.py")
hook_a = analysis("relaybay_hook.py", excludes=HOOK_EXCLUDES)

icon = str(ICONS / ("relaybay.icns" if sys.platform == "darwin" else "relaybay.ico"))


def executable(a, name, console):
    return EXE(
        PYZ(a.pure),
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        icon=icon,
        upx=False,
        argv_emulation=False,
    )


# The first executable is the macOS bundle's CFBundleExecutable: the windowed program.
desktop_exe = executable(desktop_a, "relaybay-desktop", console=False)
main_exe = executable(main_a, "relaybay", console=True)
hook_exe = executable(hook_a, "relaybay-hook", console=True)

coll = COLLECT(
    desktop_exe,
    main_exe,
    hook_exe,
    desktop_a.binaries,
    desktop_a.datas,
    main_a.binaries,
    main_a.datas,
    hook_a.binaries,
    hook_a.datas,
    name="relaybay",
    upx=False,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="relaybay.app",
        icon=icon,
        bundle_identifier="io.github.codenator81.relaybay",
        version=VERSION,
        info_plist={
            "CFBundleName": "relaybay",
            "CFBundleDisplayName": "relaybay",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
