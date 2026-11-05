"""The backup before a new version: when it is due, what it copies (WAL-safe), rotation, and
`last_version` written only after the migration succeeded."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay import __version__, backup
from relaybay.app import create_app
from relaybay.config import Settings, load_settings
from relaybay.service import Service

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def data(tmp_path: Path) -> Settings:
    settings = load_settings(home=tmp_path, environ={})
    with closing(sqlite3.connect(settings.db_path)) as conn:
        conn.execute("create table t (x text)")
        conn.execute("insert into t values ('before')")
        conn.commit()
    for name, text in (("config.toml", "port = 8765\n"), ("policy.json", "{}"), ("token", "s")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    return settings


def rows(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [r[0] for r in conn.execute("select x from t order by rowid")]


def test_nothing_without_a_database(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, environ={})
    assert backup.backup_before_upgrade(settings, "0.6.0") is None
    assert not (tmp_path / "backups").exists()


def test_first_start_with_an_old_database_backs_up(data: Settings) -> None:
    target = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert target == data.home / "backups" / "unknown-to-0.6.0-20261105-120000"
    assert sorted(p.name for p in target.iterdir()) == [
        "config.toml",
        "policy.json",
        "relaybay.sqlite",
        "token",
    ]
    assert rows(target / "relaybay.sqlite") == ["before"]


def test_same_version_copies_nothing(data: Settings) -> None:
    backup.record_version(data.home, "0.6.0")
    assert backup.backup_before_upgrade(data, "0.6.0") is None
    assert not (data.home / "backups").exists()


def test_new_version_names_both(data: Settings) -> None:
    backup.record_version(data.home, "0.5.0")
    target = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert target is not None and target.name == "0.5.0-to-0.6.0-20261105-120000"


def test_keep_zero_takes_none(data: Settings) -> None:
    data.backup_keep = 0
    assert backup.backup_before_upgrade(data, "0.6.0") is None


def test_live_wal_is_included(data: Settings) -> None:
    with closing(sqlite3.connect(data.db_path)) as live:
        live.execute("pragma journal_mode=wal")
        live.execute("pragma wal_autocheckpoint=0")
        live.execute("insert into t values ('in the wal')")
        live.commit()
        assert Path(f"{data.db_path}-wal").stat().st_size > 0
        target = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert target is not None
    assert rows(target / "relaybay.sqlite") == ["before", "in the wal"]


def test_rotation_keeps_the_newest_and_leaves_other_folders(data: Settings) -> None:
    data.backup_keep = 2
    handmade = data.home / "backup-2026-10-01"
    handmade.mkdir()
    foreign = data.home / "backups" / "my-notes"
    foreign.mkdir(parents=True)
    made = []
    for i, version in enumerate(("0.6.0", "0.6.1", "0.6.2")):
        made.append(backup.backup_before_upgrade(data, version, now=T0 + timedelta(hours=i)))
        backup.record_version(data.home, version)
    kept = sorted(p.name for p in (data.home / "backups").iterdir())
    assert kept == sorted([made[1].name, made[2].name, "my-notes"])  # type: ignore[union-attr]
    assert handmade.is_dir()


def test_failed_copy_leaves_no_partial_folder(
    data: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(backup.shutil, "copy2", broken)
    with pytest.raises(OSError, match="disk full"):
        backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert list((data.home / "backups").iterdir()) == []


def test_server_start_backs_up_and_records_the_version(data: Settings) -> None:
    backup.record_version(data.home, "0.0.1")
    with TestClient(create_app(data)):
        pass
    assert backup.last_version(data.home) == __version__
    assert [
        p.name.startswith(f"0.0.1-to-{__version__}-") for p in (data.home / "backups").iterdir()
    ] == [True]
    with TestClient(create_app(data)):  # the same version again: no second backup
        pass
    assert len(list((data.home / "backups").iterdir())) == 1


def test_version_recorded_only_after_a_successful_migration(
    data: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup.record_version(data.home, "0.0.1")

    async def failed_migration(self: Service) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(Service, "start", failed_migration)
    with pytest.raises(RuntimeError, match="migration failed"), TestClient(create_app(data)):
        pass
    assert backup.last_version(data.home) == "0.0.1"  # the next start backs up again
    assert len(list((data.home / "backups").iterdir())) == 1


# --- review fixes ----------------------------------------------------------------


def test_rotation_touches_only_our_names(tmp_path: Path) -> None:
    for name in (
        "my-copy-to-usb-20200101-000000",
        "backup-20200101-000000",
        "0.5.0-to-0.6.0-20261101-000000",
        "unknown-to-0.6.0-20261102-000000-2",
    ):
        (tmp_path / name).mkdir()
    deleted = backup.rotate(tmp_path, 1)
    assert [p.name for p in deleted] == ["0.5.0-to-0.6.0-20261101-000000"]
    assert (tmp_path / "my-copy-to-usb-20200101-000000").is_dir()


def test_a_folder_that_cannot_be_deleted_never_stops_the_start(
    data: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    data.backup_keep = 1
    folder = data.home / "backups"
    locked = folder / "0.3.0-to-0.4.0-20200101-000000"
    other = folder / "0.4.0-to-0.5.0-20200102-000000"
    locked.mkdir(parents=True)
    other.mkdir()
    real = backup.shutil.rmtree

    def rmtree(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == locked:
            raise PermissionError("in use")
        real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup.shutil, "rmtree", rmtree)
    target = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert target is not None and target.is_dir()
    assert locked.is_dir() and not other.exists()  # one locked folder, the others still go


def test_same_step_is_backed_up_once(data: Settings) -> None:
    backup.record_version(data.home, "0.5.0")
    first = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    again = backup.backup_before_upgrade(data, "0.6.0", now=T0 + timedelta(minutes=1))
    assert first is not None and again is None
    assert [p.name for p in (data.home / "backups").iterdir()] == [first.name]


def test_repeated_failed_migrations_keep_the_real_copy(
    data: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    data.backup_keep = 1
    backup.record_version(data.home, "0.0.1")

    async def failed_migration(self: Service) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(Service, "start", failed_migration)
    for _ in range(3):
        with pytest.raises(RuntimeError), TestClient(create_app(data)):
            pass
    names = [p.name for p in (data.home / "backups").iterdir()]
    assert len(names) == 1 and names[0].startswith(f"0.0.1-to-{__version__}-")


def test_taken_name_gets_a_suffix(data: Settings) -> None:
    folder = data.home / "backups"
    folder.mkdir()
    (folder / "unknown-to-0.6.0-20261105-120000").write_text("not a folder", encoding="utf-8")
    target = backup.backup_before_upgrade(data, "0.6.0", now=T0)
    assert target is not None and target.name == "unknown-to-0.6.0-20261105-120000-2"


async def test_import_apply_backs_up_before_it_migrates(data: Settings) -> None:
    from relaybay.markdown_import import open_board

    backup.record_version(data.home, "0.0.1")
    async with open_board(data):
        pass
    names = [p.name for p in (data.home / "backups").iterdir()]
    assert len(names) == 1 and names[0].startswith(f"0.0.1-to-{__version__}-")
    assert backup.last_version(data.home) == __version__
