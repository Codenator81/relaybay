"""E-mail watcher for relaybay intake: polls an IMAP folder for unseen mail, standard library
only. `thread` is the root of `References` (else `In-Reply-To`, else the message's own id),
`author` the display name of `From`; only the text/plain part is sent, capped. The mailbox password
stays here; relaybay never sees it.

Variables: IMAP_HOST, IMAP_USER, IMAP_PASSWORD, RELAYBAY_URL, RELAYBAY_TOKEN, optional IMAP_FOLDER
(INBOX), IMAP_POLL_SECONDS (60), INTAKE_SOURCE (`team-mail`)."""

import email
import email.policy
import hashlib
import imaplib
import os
import sys
import time
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relaybay_client import RelaybayClient

REQUIRED = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "RELAYBAY_URL", "RELAYBAY_TOKEN")
TEXT_MAX = 4000


def _ids(value: str | None) -> list[str]:
    return [part for part in (value or "").split() if part.startswith("<")]


def parse_message(raw: bytes) -> dict | None:
    """The `intake_post` arguments for one message, or None when it has no plain text."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    part = message.get_body(preferencelist=("plain",))
    text = part.get_content().strip() if part is not None else ""
    if not text:
        return None
    own = (message.get("Message-ID") or "").strip()
    root = (_ids(message.get("References")) or _ids(message.get("In-Reply-To")) or [own])[0]
    name, address = parseaddr(message.get("From") or "")
    received = None
    if message.get("Date"):
        try:
            received = parsedate_to_datetime(message["Date"]).isoformat()
        except (TypeError, ValueError):
            received = None
    return {
        "external_id": own or f"sha256:{hashlib.sha256(raw).hexdigest()[:32]}",
        "text": text[:TEXT_MAX],
        "author": name or address.split("@")[0] or None,
        "thread": root or None,
        "received_at": received,
        "kind": "email",
    }


def missing_variables(environ: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED if not environ.get(name)]


def main() -> int:
    missing = missing_variables(dict(os.environ))
    if missing:
        print(f"email_watcher: set {', '.join(missing)} first.", file=sys.stderr)
        return 2
    client = RelaybayClient.from_env()
    source = os.environ.get("INTAKE_SOURCE", "team-mail")
    pause = int(os.environ.get("IMAP_POLL_SECONDS", "60"))
    while True:
        with imaplib.IMAP4_SSL(os.environ["IMAP_HOST"]) as box:
            box.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
            box.select(os.environ.get("IMAP_FOLDER", "INBOX"))
            _, found = box.search(None, "UNSEEN")
            for number in found[0].split():
                _, data = box.fetch(number, "(RFC822)")  # marks it seen
                raw = data[0][1] if data and isinstance(data[0], tuple) else b""
                arguments = parse_message(raw) if raw else None
                if arguments is not None:
                    print(client.call("intake_post", source=source, **arguments))
        time.sleep(pause)


if __name__ == "__main__":
    sys.exit(main())
