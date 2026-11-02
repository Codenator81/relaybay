"""Token, cookie session, Host/Origin checks and CSRF for a server that listens on localhost."""

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("relaybay")

COOKIE_NAME = "relaybay_session"
CSRF_FIELD = "csrf_token"

CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none';"
    " frame-ancestors 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # Not "no-referrer": with it browsers send `Origin: null` on same-origin POSTs.
    (b"referrer-policy", b"same-origin"),
]

TOKEN_PREFIXES = ("/mcp", "/api", "/hooks")
PUBLIC_PATHS = ("/healthz", "/login", "/favicon.ico")
PUBLIC_PREFIXES = ("/static/",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


# --- token -----------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    """Write `text` so the file is never readable by others, not even briefly.

    A temp file is created with mode 0600 (O_EXCL) and renamed over the target; on Windows the mode
    is ignored and the user profile ACL protects the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)  # explicit, whatever the umask
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_or_create_token(path: Path) -> str:
    if path.is_file():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            logger.warning(
                "Token file %s is readable by other users; run 'chmod 600 %s'.", path, path
            )
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return rotate_token(path)


def rotate_token(path: Path) -> str:
    token = secrets.token_urlsafe(32)
    _write_private(path, token)
    return token


def _derive(token: str, purpose: str) -> str:
    return hmac.new(token.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def token_id(token: str) -> str:
    """Short public fingerprint of the token: `relaybay token` compares it with the running server
    to catch a different data folder or a rotation without restart. Reveals nothing about it."""
    return _derive(token, "relaybay-token-id")[:12]


def session_cookie_value(token: str) -> str:
    return _derive(token, "relaybay-session")


def csrf_token(token: str) -> str:
    return _derive(token, "relaybay-csrf")


def same_secret(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


# --- Host / Origin ---------------------------------------------------------


def host_name(host_header: str | None) -> str | None:
    """`127.0.0.1:8765` → `127.0.0.1`, `[::1]:8765` → `[::1]`."""
    if not host_header:
        return None
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else None
    return host.split(":", 1)[0]


def origin_host(origin: str) -> str | None:
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return host_name(parts.netloc)


class SecurityMiddleware:
    """Pure ASGI so streaming responses (SSE, MCP) pass through untouched."""

    def __init__(self, app: ASGIApp, *, token: str, allowed_hosts: list[str]) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = set(allowed_hosts)
        self.cookie = session_cookie_value(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], *SECURITY_HEADERS]
            await send(message)

        rejection = self.check(scope)
        if rejection is not None:
            await rejection(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)

    def check(self, scope: Scope) -> Response | None:
        headers = Headers(scope=scope)
        path: str = scope["path"]
        method: str = scope["method"]

        if host_name(headers.get("host")) not in self.allowed_hosts:
            return PlainTextResponse("Host not allowed", status_code=403)

        origin = headers.get("origin")
        foreign = origin is not None and origin_host(origin) not in self.allowed_hosts
        if foreign and (method not in SAFE_METHODS or path.startswith(TOKEN_PREFIXES)):
            return PlainTextResponse("Origin not allowed", status_code=403)

        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return None

        bearer = _bearer(headers.get("authorization"))
        if path.startswith(TOKEN_PREFIXES):
            if not same_secret(bearer, self.token):
                return PlainTextResponse(
                    "Missing or invalid token. Send 'Authorization: Bearer <token>';"
                    " run 'relaybay token' to see it.",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None

        if same_secret(bearer, self.token):
            # A browser never attaches this header on its own, so CSRF does not apply.
            return None
        cookie = _cookie(headers.get("cookie"), COOKIE_NAME)
        if not same_secret(cookie, self.cookie):
            if method == "GET" and _wants_page(headers):
                return RedirectResponse(login_url(_path_with_query(scope)), status_code=303)
            return PlainTextResponse(
                "Not signed in. Open the login URL that 'relaybay serve' printed"
                " (or run 'relaybay token').",
                status_code=401,
            )

        if method not in SAFE_METHODS and not same_origin(headers):
            return PlainTextResponse("Cross-origin request refused", status_code=403)
        return None


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, credential = value.partition(" ")
    return credential.strip() if scheme.lower() == "bearer" else None


def _cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _wants_page(headers: Headers) -> bool:
    """A top-level browser navigation, not an htmx fragment or an EventSource."""
    return "hx-request" not in headers and "text/html" in headers.get("accept", "")


def _path_with_query(scope: Scope) -> str:
    query = scope.get("query_string", b"").decode("latin-1")
    return scope["path"] + (f"?{query}" if query else "")


def safe_next(value: str | None) -> str:
    """A local path to return to after login; anything else falls back to `/`."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if value.split("?", 1)[0].rstrip("/") == "/login":
        return "/"
    return value


def login_url(next_path: str) -> str:
    next_path = safe_next(next_path)
    return "/login" if next_path == "/" else f"/login?next={quote(next_path, safe='/')}"


def same_origin(headers: Headers) -> bool:
    origin = headers.get("origin")
    if origin is None:
        return headers.get("sec-fetch-site") == "same-origin"
    return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()
