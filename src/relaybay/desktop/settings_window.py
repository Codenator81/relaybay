"""The program's Settings form: a local page (in external mode the board is remote), talking to
Python through pywebview's js_api.

Only public methods of `SettingsApi` reach the page; pywebview also walks public attributes, so
every attribute here starts with an underscore.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from relaybay.desktop.settings import (
    SERVER_MODES,
    DesktopSettings,
    check_url,
    cleartext_warning,
    data_dir_path,
    external_token_path,
    load_desktop_settings,
    save_desktop_settings,
    settings_path,
    write_external_token,
)
from relaybay.desktop.window import background_color
from relaybay.machine_setup import SetupError, format_plan
from relaybay.web.routes import STATIC_DIR

if TYPE_CHECKING:
    from relaybay.desktop.app import DesktopController


def page_html() -> str:
    html = (Path(__file__).with_name("settings.html")).read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    return html.replace("/*APP_CSS*/", css)


def apply_form(folder: Path, data: dict[str, Any]) -> tuple[DesktopSettings, DesktopSettings]:
    """Validate the form and write desktop.toml (and the token); (before, after). Raises
    ValueError with a message for the form."""
    before = load_desktop_settings(folder)
    mode = str(data.get("server_mode", "embedded"))
    if mode not in SERVER_MODES:
        raise ValueError(f"Unknown server mode {mode!r}.")
    data_dir = str(data.get("data_dir") or "").strip()
    url = str(data.get("server_url") or "").strip()
    token = str(data.get("token") or "").strip()
    after = DesktopSettings(
        data_dir=data_dir_path(data_dir) if data_dir else None,
        autostart=bool(data.get("autostart")),
        server_mode=mode,
        server_url=check_url(url) if url else "",
        update_check=bool(data.get("update_check", before.update_check)),
        update_check_hours=before.update_check_hours,
        notify=before.notify,
        notify_quiet=before.notify_quiet,
        notify_kinds=before.notify_kinds,
    )
    if mode == "external":
        if not after.server_url:
            raise ValueError("An external server needs its URL.")
        if not token and not external_token_path(folder).is_file():
            raise ValueError("An external server needs its token (run 'relaybay token' there).")
    if token:
        write_external_token(folder, token)
    save_desktop_settings(folder, after)
    return before, after


class SettingsApi:
    def __init__(self, controller: "DesktopController") -> None:
        self._controller = controller
        self._window: Any = None

    def get(self) -> dict[str, Any]:
        folder = self._controller.config_dir
        current = load_desktop_settings(folder)
        return {
            "path": str(settings_path(folder)),
            "data_dir": str(current.data_dir or ""),
            "server_mode": current.server_mode,
            "server_url": current.server_url,
            "autostart": self._controller.autostart.is_enabled(),
            "update_check": current.update_check,
        }

    def browse(self, start: str) -> str | None:
        import webview

        assert self._window is not None
        chosen = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=start or str(Path.home())
        )
        return str(chosen[0]) if chosen else None

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        controller = self._controller
        try:
            before, after = apply_form(controller.config_dir, data)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        controller.set_autostart(after.autostart)
        controller.desktop.update_check = after.update_check
        server_changed = (before.data_dir, before.server_mode, before.server_url) != (
            after.data_dir,
            after.server_mode,
            after.server_url,
        ) or bool(str(data.get("token") or "").strip())
        warning = cleartext_warning(after)
        note = f" Warning: {warning}" if warning else ""
        if not server_changed:
            return {"ok": True, "message": "Saved." + note}
        controller.restart_async()
        return {"ok": True, "message": "Saved. The server restarts with the new settings." + note}

    def reconnect_plan(self) -> dict[str, Any]:
        """The plan and diff, exactly as `relaybay setup` prints them; nothing is written."""
        try:
            plan = self._controller.reconnect_plan()
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        hooks = plan.hooks
        return {
            "ok": plan.error is None,
            "text": "\n".join(format_plan(plan)),
            "hash": hooks.plan_hash if hooks is not None and hooks.changed else "",
            "message": plan.error or (hooks.summary if hooks is not None else ""),
        }

    def reconnect_apply(self, expect: str) -> dict[str, Any]:
        try:
            message = self._controller.reconnect_apply(str(expect))
        except (SetupError, ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": message}

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


def open_settings(controller: "DesktopController", existing: Any) -> Any:
    """Show the Settings window, creating it when needed; returns the window."""
    import webview

    if existing is not None and existing in webview.windows:
        existing.show()
        existing.restore()
        return existing
    api = SettingsApi(controller)
    window = webview.create_window(
        "relaybay settings",
        html=page_html(),
        js_api=api,
        width=640,
        height=600,
        background_color=background_color(),
    )
    api._window = window
    return window
