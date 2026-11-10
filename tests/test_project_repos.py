"""Project repositories, a task's code links (auto, agent, owner), its pull requests, the Code row
on the board, and their way through export and import."""

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.db.connection import connect, migrate
from relaybay.domain.code import (
    REPO_NAME_RE,
    default_repo_name,
    holds,
    pr_number,
    pr_repo_name,
    vscode_url,
)
from relaybay.domain.errors import ValidationError
from relaybay.markdown_export import gather
from relaybay.markdown_import import apply_plan, make_plan
from relaybay.mcp_server.server import build_mcp
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_migrations import LATEST, NOW

FORM = {"Origin": BASE}
CC = "6c1f0a52-2b7e-4d1a-9f43-0e5b8a7c9d11"
DESKTOP = "local_00000000-0000-0000-0000-0000000000a1"
ROOT = "/home/dev/acme-shop"
REPOS = [
    {"name": "web", "path": f"{ROOT}/web", "kind": "web"},
    {"name": "api", "path": f"{ROOT}/api", "kind": "api"},
    {"path": f"{ROOT}/mobile"},
]


def test_value_helpers() -> None:
    assert vscode_url("C:\\code\\acme shop\\web") == "vscode://file/C:/code/acme%20shop/web"
    assert vscode_url("/home/dev/a#b") == "vscode://file/home/dev/a%23b"
    assert pr_number("https://github.com/acme/web/pull/12") == 12
    assert pr_number("https://gitlab.com/acme/web/-/merge_requests/7/diffs") == 7
    assert pr_number("https://example.com/acme/web") is None
    assert pr_repo_name("https://github.com/acme/Web/pull/12") == "web"
    assert pr_repo_name("https://gitlab.com/acme/api/-/merge_requests/7") == "api"
    assert default_repo_name("C:\\code\\acme-shop\\", "x") == "acme-shop"
    assert default_repo_name("/", "fallback") == "fallback"
    assert holds("/home/dev/acme-shop", "/home/dev/acme-shop/web/src") is not None
    assert holds("/home/dev/acme-shop", "/home/dev/acme-shop-2") is None
    # `~nouser` is never expanded (POSIX would raise for an unknown user): no match, no error.
    assert holds("~nouser-relaybay/x", "/home/dev/acme-shop") is None
    assert holds("~/acme-shop", str(Path.home() / "acme-shop" / "web")) is not None


async def test_repos_replace_by_name_and_repo_path_follows_the_first(service: Service) -> None:
    project, _ = await service.upsert_project("acme-shop", repos=REPOS)
    _, repos = await service.project_repos("acme-shop")
    assert [(r.name, r.kind) for r in repos] == [("web", "web"), ("api", "api"), ("mobile", None)]
    assert project.repo_path == f"{ROOT}/web"
    task = await service.create_task("acme-shop", "Checkout")
    await service.update_task(task.key, {"repos": ["api"]})
    # api keeps its row (and the task's link) when the list is written again in another order.
    project, _ = await service.upsert_project(
        "acme-shop", repos=[REPOS[1], {"name": "web", "path": f"{ROOT}/web2"}]
    )
    _, repos = await service.project_repos("acme-shop")
    assert [(r.name, r.path) for r in repos] == [("api", f"{ROOT}/api"), ("web", f"{ROOT}/web2")]
    assert project.repo_path == f"{ROOT}/api"
    code = (await service.task_detail(task.key)).code
    assert [r.repo.name for r in code.repos] == ["api"]
    # repo_path alone moves the first repository.
    project, _ = await service.upsert_project("acme-shop", repo_path=f"{ROOT}/api-v2")
    _, repos = await service.project_repos("acme-shop")
    assert repos[0].path == f"{ROOT}/api-v2" and project.repo_path == f"{ROOT}/api-v2"
    with pytest.raises(ValidationError, match="unique"):
        await service.upsert_project("acme-shop", repos=[REPOS[0], REPOS[0]])
    with pytest.raises(ValidationError, match="kind must be one of"):
        await service.upsert_project("acme-shop", repos=[{"path": "/x", "kind": "db"}])
    with pytest.raises(ValidationError, match="first repository's path"):
        await service.upsert_project("acme-shop", repos=REPOS, repo_path="/elsewhere")


async def test_a_new_project_with_repo_path_gets_one_repository(service: Service) -> None:
    project, _ = await service.upsert_project("demo-api", repo_path="C:\\code\\demo-api")
    _, repos = await service.project_repos("demo-api")
    assert [(r.name, r.path) for r in repos] == [("demo-api", "C:\\code\\demo-api")]
    assert project.repo_path == "C:\\code\\demo-api"
    # With every repository removed, the same repo_path adds one again.
    await service.upsert_project("demo-api", repos=[])
    assert (await service.project_repos("demo-api"))[1] == []
    await service.upsert_project("demo-api", repo_path="C:\\code\\demo-api")
    _, repos = await service.project_repos("demo-api")
    assert [(r.name, r.path) for r in repos] == [("demo-api", "C:\\code\\demo-api")]


async def test_agent_links_repos_and_prs(service: Service) -> None:
    await service.upsert_project("acme-shop", repos=REPOS)
    task = await service.create_task("acme-shop", "Checkout")
    before = await service.last_event_id()
    await service.update_task(
        task.key,
        {
            "repos": ["web"],
            "prs": [
                "https://github.com/acme/api/pull/31",  # names api: tied to it, api linked
                "mobile https://example.com/review/5",
                "https://example.com/changes/9",
            ],
        },
        session="dev-1",
    )
    code = (await service.task_detail(task.key)).code
    assert [(r.repo.name, r.how, [p.label for p in r.prs]) for r in code.repos] == [
        ("web", "agent", []),
        ("api", "agent", ["#31"]),
        ("mobile", "agent", ["https://example.com/review/5"]),
    ]
    assert [p.url for p in code.loose_prs] == ["https://example.com/changes/9"]
    assert code.summary == "Code: 3 repos, 3 PRs"
    [event] = [e for e in await service.replay(before, None, 50) if e.kind == "task.updated"]
    assert event.payload["changes"]["repos"] == {"added": ["web", "api", "mobile"]}
    assert len(event.payload["changes"]["prs"]["added"]) == 3

    await service.update_task(
        task.key, {"repos": ["-mobile", "web"], "prs": ["-https://example.com/changes/9"]}
    )
    code = (await service.task_detail(task.key)).code
    assert [r.repo.name for r in code.repos] == ["web", "api"]
    # The mobile PR stays on the task, tied to a repository the task no longer lists.
    assert [p.url for p in code.loose_prs] == ["https://example.com/review/5"]
    with pytest.raises(ValidationError, match="Its repositories: web, api, mobile"):
        await service.update_task(task.key, {"repos": ["shop"]})
    with pytest.raises(ValidationError, match="http"):
        await service.update_task(task.key, {"prs": ["ftp://example.com/pull/1"]})


async def test_a_project_without_repos_says_how_to_add_them(service: Service) -> None:
    await service.upsert_project("demo-api")
    task = await service.create_task("demo-api", "Ping")
    with pytest.raises(ValidationError, match="project_upsert"):
        await service.update_task(task.key, {"repos": ["api"]})
    code = (await service.task_detail(task.key)).code
    assert not code.shown


async def test_hook_links_the_closest_repo_of_the_bound_task(service: Service) -> None:
    await service.upsert_project(
        "acme-shop",
        repos=[{"name": "shop", "path": ROOT}, {"name": "web", "path": f"{ROOT}/web"}],
    )
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP, hook_session=CC
    )
    before = await service.last_event_id()
    hook = {
        "session_id": CC,
        "cwd": f"{ROOT}/web/src",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "go",
    }
    await service.record_hook(hook)
    code = (await service.task_detail(task.key)).code
    assert [(r.repo.name, r.how) for r in code.repos] == [("web", "auto")]
    events = [e for e in await service.replay(before, None, 50) if e.kind == "task.updated"]
    assert [(e.payload["changes"], e.payload["how"]) for e in events] == [
        ({"repos": {"added": ["web"]}}, "auto")
    ]
    # The same folder again writes nothing new.
    await service.record_hook({**hook, "hook_event_name": "Stop"})
    assert len([e for e in await service.replay(before, None, 50) if e.kind == "task.updated"]) == 1
    # An automatic link is not news in the digests.
    digest = await service.task_context(task.key, since_event=before)
    assert "task.updated" not in digest


async def test_a_failing_auto_link_never_fails_the_hook(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.upsert_project(
        "acme-shop",
        repos=[{"name": "odd", "path": "~nouser-relaybay/shop"}, {"name": "web", "path": ROOT}],
    )
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP, hook_session=CC
    )
    hook = {"session_id": CC, "cwd": f"{ROOT}/src", "hook_event_name": "UserPromptSubmit"}
    # A `~user` path is no match and no error: the closest real one is linked.
    await service.record_hook(hook)
    assert [r.repo.name for r in (await service.task_detail(task.key)).code.repos] == ["web"]

    async def broken(*_: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("relaybay.db.repo.session_repo_candidates", broken)
    session = await service.record_hook({**hook, "hook_event_name": "Stop"})
    assert session is not None and session.status == "idle"


async def test_migration_seeds_one_repo_per_project(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0029")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 28;\nCOMMIT;")
        for slug, path in (
            ("acme-shop", "C:\\code\\acme-shop\\"),
            ("demo-api", "/home/dev/demo-api"),
            ("root", "/"),
            ("spaced", "/home/dev/My Repo"),
            ("drive", "C:\\"),
            ("naive", "/home/dev/naïve"),
            ("unnamed", "/home/dev/_x"),
            ("none", None),
        ):
            await conn.execute(
                "INSERT INTO project (slug, name, repo_path, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (slug, slug, path, NOW, NOW),
            )
        await conn.commit()
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()
    with sqlite3.connect(db) as plain:
        rows = plain.execute(
            "SELECT p.slug, r.name, r.path, r.position FROM project_repo r"
            " JOIN project p ON p.id = r.project_id ORDER BY p.slug"
        ).fetchall()
    assert rows == [
        ("acme-shop", "acme-shop", "C:\\code\\acme-shop\\", 0),
        ("demo-api", "demo-api", "/home/dev/demo-api", 0),
        ("drive", "drive", "C:\\", 0),
        ("naive", "na-ve", "/home/dev/naïve", 0),
        ("root", "root", "/", 0),
        ("spaced", "My-Repo", "/home/dev/My Repo", 0),
        ("unnamed", "unnamed", "/home/dev/_x", 0),
    ]
    # Every seeded name is one a write accepts.
    assert all(REPO_NAME_RE.match(name) for _, name, _, _ in rows)


def test_board_repositories_and_code_row(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    form = {"csrf_token": csrf_token(token), "name": "web", "path": "C:\\code\\acme\\web"}
    bearer = board.post(
        "/p/acme-shop/repos", data=form, headers={**FORM, "Authorization": f"Bearer {token}"}
    )
    assert bearer.status_code == 403
    bad_csrf = board.post("/p/acme-shop/repos", data={**form, "csrf_token": "x"}, headers=FORM)
    assert bad_csrf.status_code == 403
    added = board.post("/p/acme-shop/repos", data=form, headers=FORM, follow_redirects=False)
    assert added.status_code == 303
    second = {**form, "name": "", "path": "/home/dev/api", "kind": "api"}
    assert board.post("/p/acme-shop/repos", data=second, headers=FORM).status_code == 200
    _, repos = run_in_app(board, service.project_repos, "acme-shop")
    assert [(r.name, r.kind) for r in repos] == [("web", None), ("api", "api")]
    page = board.get("/p/acme-shop/environments")
    assert "Repositories" in page.text and "vscode://file/C:/code/acme/web" in page.text
    moved = board.post(
        f"/p/acme-shop/repos/{repos[1].id}/up", data={"csrf_token": csrf_token(token)}, headers=FORM
    )
    assert moved.status_code == 200
    project, repos = run_in_app(board, service.project_repos, "acme-shop")
    assert [r.name for r in repos] == ["api", "web"] and project.repo_path == "/home/dev/api"
    clash = board.post(
        f"/p/acme-shop/repos/{repos[1].id}",
        data={**form, "name": "api"},
        headers=FORM,
    )
    assert clash.status_code == 400 and "already has a repository" in clash.text

    task = run_in_app(board, service.create_task, "acme-shop", "Checkout", branch="feat/pay")
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert "Repos: all" in card and "Code: all repos" in card
    run_in_app(
        board,
        service.update_task,
        task.key,
        {"repos": ["web"], "prs": ["https://github.com/acme/web/pull/12"]},
    )
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert "Open in VS Code" in card and 'href="vscode://file/C:/code/acme/web"' in card
    assert ">#12</a>" in card and "Code: 1 repo, 1 PR" in card and "Repos: all" not in card
    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "<dt>Code</dt>" in view and "feat/pay" in view
    assert "Code: 1 repo, 1 PR" in board.get("/p/acme-shop").text

    removed = board.post(
        f"/p/acme-shop/repos/{repos[1].id}/delete",
        data={"csrf_token": csrf_token(token)},
        headers=FORM,
    )
    assert removed.status_code == 200
    code = run_in_app(board, service.task_detail, task.key).code
    assert code.repos == [] and [p.label for p in code.loose_prs] == ["#12"]


async def test_mcp_tools_take_repos_and_prs(service: Service) -> None:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    saved = await call("project_upsert", slug="acme-shop", repos=REPOS[:2])
    assert "repos: web (web) `/home/dev/acme-shop/web`, api (api)" in saved
    await call("task_create", project="acme-shop", title="Checkout")
    await call(
        "task_update",
        task="acme-shop-1",
        repos=["web"],
        prs=["https://github.com/acme/web/pull/12"],
    )
    await call("task_patch", task="acme-shop-1", fields={"repos": ["api"]})
    detail = await call("task_get", task="acme-shop-1")
    assert "## Code" in detail and "PR #12 https://github.com/acme/web/pull/12" in detail
    assert "- api (api) `/home/dev/acme-shop/api` (linked: agent)" in detail
    assert detail.rstrip().endswith("tracker_logged, repos, prs")
    with pytest.raises(ToolError, match="Its repositories: web, api"):
        await mcp.call_tool("task_update", {"task": "acme-shop-1", "repos": ["shop"]})
    schema = await mcp.read_resource("relaybay://schema/task")
    assert "`repos`" in str(schema) and "`prs`" in str(schema)


async def test_export_and_import_carry_repos_and_links(tmp_path: Path) -> None:
    source = Service(tmp_path / "a.sqlite")
    await source.start()
    try:
        await source.upsert_project("acme-shop", repos=REPOS)
        task = await source.create_task("acme-shop", "Checkout")
        await source.update_task(
            task.key,
            {"repos": ["api"], "prs": ["https://github.com/acme/web/pull/12", "https://x.test/9"]},
        )
        wanted, problems = await gather(source)
    finally:
        await source.close()
    assert not problems
    text = wanted["projects/acme-shop/code.md"].text
    assert "| web | web | /home/dev/acme-shop/web |" in text
    assert (
        f"| {task.key} | web, api | web https://github.com/acme/web/pull/12<br>https://x.test/9 |"
        in text
    )
    folder = tmp_path / "out"
    for rel, item in wanted.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(item.text, encoding="utf-8")

    target = Service(tmp_path / "b.sqlite")
    await target.start()
    try:
        plan = await make_plan(target, folder)
        assert not plan.failed, [
            (i.kind, i.item, i.detail) for i in plan.items if i.action == "error"
        ]
        assert {i.item for i in plan.items if i.kind == "repo"} == {
            "acme-shop/web",
            "acme-shop/api",
            "acme-shop/mobile",
        }
        await apply_plan(target, plan)
        assert not [i for i in plan.items if i.action == "error"]
        _, repos = await target.project_repos("acme-shop")
        assert [(r.name, r.kind) for r in repos] == [
            ("web", "web"),
            ("api", "api"),
            ("mobile", None),
        ]
        [imported] = await target.list_tasks(project="acme-shop")
        code = await target.task_code(imported)
        assert [(r.repo.name, [p.label for p in r.prs]) for r in code.repos] == [
            ("web", ["#12"]),
            ("api", []),
        ]
        assert [p.url for p in code.loose_prs] == ["https://x.test/9"]
        again = await make_plan(target, folder)
        assert not again.writes
    finally:
        await target.close()
