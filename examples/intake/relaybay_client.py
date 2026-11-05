"""A minimal MCP client for relaybay's /mcp endpoint, standard library only. Reads RELAYBAY_URL
(e.g. http://127.0.0.1:8765) and RELAYBAY_TOKEN (the board token, `relaybay token`)."""

import json
import os
import urllib.request


class RelaybayClient:
    def __init__(self, url: str, token: str) -> None:
        self.endpoint = url.rstrip("/") + "/mcp"
        self.token = token
        self.session: str | None = None
        self.next_id = 0

    @classmethod
    def from_env(cls) -> "RelaybayClient":
        missing = [n for n in ("RELAYBAY_URL", "RELAYBAY_TOKEN") if not os.environ.get(n)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} first.")
        return cls(os.environ["RELAYBAY_URL"], os.environ["RELAYBAY_TOKEN"])

    def _send(self, method: str, params: dict, notify: bool = False) -> dict:
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Mcp-Session-Id": self.session} if self.session else {}),
        }
        request = urllib.request.Request(self.endpoint, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            self.session = response.headers.get("Mcp-Session-Id") or self.session
            text = response.read().decode("utf-8")
        if notify or not text.strip():
            return {}
        # A streamable HTTP answer is JSON or one SSE event whose data line holds the JSON.
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        answer = json.loads(data[-1] if data else text)
        if "error" in answer:
            raise RuntimeError(answer["error"].get("message", "MCP error"))
        return answer["result"]

    def connect(self) -> None:
        info = {"name": "relaybay-intake-example", "version": "1"}
        self._send(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": info}
        )
        self._send("notifications/initialized", {}, notify=True)

    def call(self, tool: str, **arguments: object) -> str:
        if self.session is None:
            self.connect()
        result = self._send("tools/call", {"name": tool, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(text)
        return text
