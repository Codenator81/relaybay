# Recording the README GIF

The README shows `docs/assets/board.gif`: the board updating live while sessions report their
work. It is recorded by hand from the demo workspace, so it never shows real projects, paths or
tokens. Re-record it when the board changes visibly.

## Prepare

```bash
relaybay demo --home ./gif-data --force
relaybay serve --home ./gif-data --port 8766
```

- Open the login link the server prints in a fresh browser profile (no bookmarks bar, no
  extensions, light theme), then open `http://127.0.0.1:8766/p/acme-shop`. The address bar must not
  show the token; reload once after the login redirect if it does.
- Window size 1280×720, browser zoom 100%. Close every other tab and notification.
- Keep the Claude Code session in another window, outside the recorded area.

## Script (about 20 seconds)

1. Two seconds on the `acme-shop` board: the "One-page checkout" card in `dev` shows its working
   session dot and the `SHOP-42` badge.
2. Move a task while the board is visible, from a Claude Code session connected to the demo
   server (`relaybay token --home ./gif-data` prints the `claude mcp add` line; change the port to
   8766, the scope to `--scope local` and the name to `relaybay-demo` so your real entry stays). Ask it: "Move
   acme-shop-2 to test with a report `verified · M · checkout on one page`". Record only the
   browser window, never the terminal with the token.
3. The card moves from `dev` to `test` without a reload; hold two seconds.
4. Click the card: the task dialog with description, progress and the new report; hold three
   seconds; close it with Esc.
5. Optional: narrow the window to a phone width to show the stage tab bar.

## Record and export

- Any screen recorder that crops to the browser window (for example ScreenToGif on Windows,
  Kap on macOS, Peek on Linux), 15 fps.
- Export a GIF under 5 MB, 1280 px wide or less (`gifsicle -O3 --lossy=80 --colors 128` helps).
- Save it as `docs/assets/board.gif` and check it renders in the README on GitHub.
- Remove the local `claude mcp` entry and the `gif-data` folder afterwards.
