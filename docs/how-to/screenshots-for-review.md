# Screenshots for review

When a task moves to `review` or `test`, the owner accepts it step by step in the task's **Review**
tab. The agent writes the steps with `review_steps_set` and attaches the evidence with
`attach_file`. This page is for the agent (and the person who sets it up): how to produce a PNG
file that `attach_file` accepts.

A built-in browser in a desktop app usually returns a screenshot to the model only, not as a file
on disk, so it cannot be attached. Use one of the recipes below instead.

## What `attach_file` accepts

- PNG, JPEG, WebP or GIF, checked by the file's content, not its name. SVG, HTML and everything
  else are refused.
- At most 10 MB per file and 200 MB per task (`attachments_max_mb`, `attachments_task_max_mb` in
  the [configuration](../reference/configuration.md)).
- A `path` must be absolute and lie inside an allowed folder: the project's `repo_path`, the
  user's temp folder, or a folder listed in `attachment_roots`. Symlinks and `..` are resolved
  before the check. The file is **copied**; the original stays where it is.
- An agent on another machine sends the image as `content_base64` instead (same rules).

Save screenshots to the temp folder (`$TMPDIR`, `/tmp`, `%TEMP%`) or to an ignored folder of the
repository, never to a tracked one.

## Headless Chrome or Edge (no sign-in)

For pages that need no login (a local dev server, a public test page):

```bash
# Linux / macOS
google-chrome --headless --disable-gpu --window-size=1280,800 \
  --screenshot=/tmp/acme-shop-cart.png http://localhost:3000/cart
```

```powershell
# Windows (Edge ships with the system; Chrome works the same way)
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless --disable-gpu `
  --window-size=1280,800 --screenshot="$env:TEMP\acme-shop-cart.png" http://localhost:3000/cart
```

Use `--window-size=390,844` for a phone-sized shot. The browser prints
`… bytes written to file …` when it is done.

## Playwright with a saved login (local and test environments only)

For pages behind a sign-in, sign in **once** with a test account, save the login state, and then
capture pages by URL with that state. This is allowed only where the project's environment says
so: `environments_get` shows "agent may sign in and remember the login: yes" and the path of the
login state (`login_state_path`). It is never allowed on `live`.

- The saved state and the test account live **outside the project repository and outside
  relaybay's database**: in a user folder such as `~/.local/state/acme-shop/test-login.json`.
  relaybay stores only that path.
- Use a test account from the project's own fixtures, never a real person's account.

```python
# save_login.py: run once (it opens a browser window for the sign-in)
from playwright.sync_api import sync_playwright

STATE = "/home/demo/.local/state/acme-shop/test-login.json"  # login_state_path

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://staging.acme-shop.example/login")
    page.pause()  # sign in with the test account, then resume
    page.context.storage_state(path=STATE)
    browser.close()
```

```python
# shot.py URL OUT: a screenshot with the saved login
import sys
from playwright.sync_api import sync_playwright

STATE = "/home/demo/.local/state/acme-shop/test-login.json"

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(storage_state=STATE, viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(sys.argv[1], wait_until="networkidle")
    page.screenshot(path=sys.argv[2], full_page=True)
    browser.close()
```

Playwright needs `pip install playwright` and `playwright install chromium` (or
`p.chromium.launch(channel="msedge")` / `channel="chrome"` to use an installed browser). When the
saved login expires, run `save_login.py` again.

## Android emulator or device

```bash
adb exec-out screencap -p > /tmp/acme-shop-android.png
```

Drive the app with `adb shell input tap X Y`, `adb shell input text "…"` and read the screen
layout with `adb shell uiautomator dump`, or use a mobile MCP server. Windows PowerShell 5.1
re-encodes binary output redirected with `>`, which breaks the PNG; run the redirect through
`cmd` instead: `cmd /c "adb exec-out screencap -p > %TEMP%\shot.png"`.

## iOS simulator (macOS)

```bash
xcrun simctl io booted screenshot /tmp/acme-shop-ios.png
```

## Attach it

```text
attach_file(task="acme-shop-12", path="/tmp/acme-shop-cart.png", step=2,
            caption="Cart with two products; the badge shows 2")
```

The answer names the attachment id (`#7`); change the caption later with
`attachment_caption(7, "…")`. Attaching the same image to the same task again stores nothing new.

## The owner's fallback: paste

The owner can add a screenshot without any agent: in the task's **Review** tab, click into a
step's comment box and press **Ctrl+V** (⌘V on a Mac) with an image in the clipboard. It is
uploaded to that step with the same checks.
