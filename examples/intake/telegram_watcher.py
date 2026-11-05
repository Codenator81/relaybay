"""Telegram watcher for relaybay intake: Bot API long polling, standard library only. Every message
of the allowed chats goes to `intake_post` with `thread` = the chat id and `author` = the sender's
first name. The bot token stays here; relaybay never sees it.

Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHATS (comma-separated chat ids), RELAYBAY_URL,
RELAYBAY_TOKEN, optional INTAKE_SOURCE (default `telegram`)."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relaybay_client import RelaybayClient

REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHATS", "RELAYBAY_URL", "RELAYBAY_TOKEN")
TEXT_MAX = 4000
POLL_SECONDS = 50


def parse_update(update: dict, allowed: set[str]) -> dict | None:
    """The `intake_post` arguments for one update, or None (no text, or a chat not allowed)."""
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text") or message.get("caption")
    if chat not in allowed or not isinstance(text, str) or not text.strip():
        return None
    sender = message.get("from") or {}
    sent = message.get("date")
    return {
        "external_id": f"{chat}:{message.get('message_id')}",
        "text": text.strip()[:TEXT_MAX],
        "author": sender.get("first_name") or None,
        "thread": chat,
        "received_at": (
            datetime.fromtimestamp(sent, UTC).isoformat() if isinstance(sent, int) else None
        ),
        "kind": "telegram",
    }


def missing_variables(environ: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED if not environ.get(name)]


def main() -> int:
    missing = missing_variables(dict(os.environ))
    if missing:
        print(f"telegram_watcher: set {', '.join(missing)} first.", file=sys.stderr)
        return 2
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    allowed = {c.strip() for c in os.environ["TELEGRAM_CHATS"].split(",") if c.strip()}
    source = os.environ.get("INTAKE_SOURCE", "telegram")
    client = RelaybayClient.from_env()
    offset = 0
    while True:
        query = urllib.parse.urlencode({"timeout": POLL_SECONDS, "offset": offset})
        try:
            with urllib.request.urlopen(f"{api}/getUpdates?{query}", timeout=70) as response:
                updates = json.loads(response.read()).get("result", [])
        except OSError as exc:
            print(f"telegram_watcher: {exc}; retrying in 10 s", file=sys.stderr)
            time.sleep(10)
            continue
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            arguments = parse_update(update, allowed)
            if arguments is not None:
                print(client.call("intake_post", source=source, **arguments))


if __name__ == "__main__":
    sys.exit(main())
