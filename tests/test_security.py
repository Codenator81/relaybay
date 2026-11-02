import logging
import os

import pytest
from starlette.testclient import TestClient

from relaybay.logs import RedactLoginFilter, uvicorn_log_config
from relaybay.security import COOKIE_NAME, csrf_token, load_or_create_token, rotate_token


def test_healthz_is_public(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert set(response.json()) == {"status", "version", "token_id"}
    assert len(response.json()["token_id"]) == 12
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_mcp_and_api_need_a_bearer_token(client: TestClient, token: str) -> None:
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/api/projects").status_code == 401
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    # The cookie is for the browser board, not for /mcp.
    client.get(f"/login?token={token}", follow_redirects=False)
    assert client.post("/mcp", json={}).status_code == 401


def test_bad_host_is_refused(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/healthz", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "[::1]:8765"}).status_code == 200


def test_bad_origin_is_refused(client: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    evil = {**auth, "Origin": "http://evil.example"}
    assert client.post("/mcp", json={}, headers=evil).status_code == 403
    assert client.get("/mcp", headers=evil).status_code == 403
    assert client.post("/p/x/t/x-1", headers={"Origin": "http://evil.example"}).status_code == 403


def test_board_requires_login(client: TestClient, token: str) -> None:
    assert client.get("/").status_code == 401
    wrong = client.get("/login?token=wrong&next=/p/acme-shop", follow_redirects=False)
    # The token never stays in the address bar: redirect, then show the flashed error once.
    assert wrong.status_code == 303 and wrong.headers["location"] == "/login?next=/p/acme-shop"
    page = client.get(wrong.headers["location"])
    assert page.status_code == 401 and "That token is not valid" in page.text
    assert "not valid" not in client.get("/login").text

    response = client.get(f"/login?token={token}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert COOKIE_NAME in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert token not in cookie
    assert client.get("/").status_code == 200


def test_board_post_needs_same_origin_and_csrf(board: TestClient, token: str, seeded: str) -> None:
    key = seeded
    form = {"title": "Renamed", "stage": "dev", "version": "1"}
    same = {"Origin": "http://127.0.0.1:8765"}
    with_csrf = {**form, "csrf_token": csrf_token(token)}
    url = f"/p/acme-shop/t/{key}"
    assert board.post(url, data=with_csrf).status_code == 403  # cookie but no Origin
    # HX-Request alone no longer replaces the CSRF token.
    assert board.post(url, data=form, headers={**same, "HX-Request": "true"}).status_code == 403
    assert (
        board.post(f"/p/acme-shop/t/{key}", data=form, headers=same).status_code == 403
    )  # no CSRF
    ok = board.post(
        f"/p/acme-shop/t/{key}", data={**form, "csrf_token": csrf_token(token)}, headers=same
    )
    assert ok.status_code == 200


def test_token_file(tmp_path) -> None:
    path = tmp_path / "token"
    first = load_or_create_token(path)
    assert len(first) >= 43 and load_or_create_token(path) == first
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert rotate_token(path) != first


def test_bearer_post_skips_same_origin_but_not_csrf(
    client: TestClient, token: str, seeded: str
) -> None:
    url = f"/p/acme-shop/t/{seeded}"
    auth = {"Authorization": f"Bearer {token}"}
    form = {"title": "Renamed", "stage": "dev", "version": "1"}
    assert client.post(url, data=form, headers=auth).status_code == 403  # no CSRF token
    ok = client.post(url, data={**form, "csrf_token": csrf_token(token)}, headers=auth)
    assert ok.status_code == 200


def test_token_file_is_replaced_atomically(tmp_path) -> None:
    path = tmp_path / "token"
    load_or_create_token(path)
    rotate_token(path)
    assert [p.name for p in tmp_path.iterdir()] == ["token"]  # no temp files left behind


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_wide_token_permissions_warn(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "token"
    load_or_create_token(path)
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="relaybay"):
        load_or_create_token(path)
    assert "readable by other users" in caplog.text


def test_access_log_redacts_the_login_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/login?token=secret-value", "1.1", 303),
        None,
    )
    assert RedactLoginFilter().filter(record)
    assert "secret-value" not in record.getMessage()
    assert "/login?<redacted>" in record.getMessage()
    slash = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/login/?token=secret-value", "1.1", 401),
        None,
    )
    assert RedactLoginFilter().filter(slash)
    assert "secret-value" not in slash.getMessage()
    config = uvicorn_log_config()
    assert config["handlers"]["access"]["filters"] == ["redact_login"]


PAGE = {"Accept": "text/html,application/xhtml+xml"}


def test_browser_is_sent_to_the_login_page(client: TestClient) -> None:
    response = client.get("/p/acme-shop?x=1", headers=PAGE, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/p/acme-shop%3Fx%3D1"
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert 'name="token"' in page.text and "Sign in" in page.text
    assert 'name="next" value="/p/acme-shop?x=1"' in page.text
    assert "relaybay token" in page.text
    # htmx fragments and the event stream keep a plain 401.
    assert client.get("/events", headers={**PAGE, "HX-Request": "true"}).status_code == 401


def _login_form(client: TestClient, next_path: str = "/") -> str:
    page = client.get(f"/login?next={next_path}")
    marker = 'name="login_csrf" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_login_form_signs_in_and_returns(client: TestClient, token: str) -> None:
    origin = {"Origin": "http://127.0.0.1:8765"}
    nonce = _login_form(client, "/p/acme-shop")
    form = {"token": "wrong", "next": "/p/acme-shop", "login_csrf": nonce}
    bad = client.post("/login", data=form, headers=origin)
    assert bad.status_code == 401 and "That token is not valid" in bad.text

    nonce = _login_form(client, "/p/acme-shop")
    form = {"token": token, "next": "/p/acme-shop", "login_csrf": nonce}
    ok = client.post("/login", data=form, headers=origin, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/p/acme-shop"
    assert token not in ok.headers["location"]
    assert client.get("/").status_code == 200


def test_login_form_needs_its_csrf_value(client: TestClient, token: str) -> None:
    origin = {"Origin": "http://127.0.0.1:8765"}
    _login_form(client)
    form = {"token": token, "next": "/", "login_csrf": "forged"}
    response = client.post("/login", data=form, headers=origin, follow_redirects=False)
    assert response.status_code == 403
    assert client.get("/").status_code == 401


def test_login_next_stays_local() -> None:
    from relaybay.security import safe_next

    assert safe_next("/p/acme-shop") == "/p/acme-shop"
    for value in ("https://evil.example", "//evil.example", r"/\evil.example", "/login", ""):
        assert safe_next(value) == "/"
