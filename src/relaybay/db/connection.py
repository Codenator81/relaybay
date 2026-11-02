import logging
import re
from importlib import resources
from pathlib import Path

import aiosqlite

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# First line of a migration that rebuilds a table other tables reference.
FOREIGN_KEYS_OFF = "-- relaybay:foreign-keys-off"
logger = logging.getLogger("relaybay")


async def connect(path: Path) -> aiosqlite.Connection:
    # Autocommit mode: the service opens transactions explicitly with BEGIN IMMEDIATE.
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _broken_keys(conn: aiosqlite.Connection) -> set[tuple[str, int, str, int]]:
    """Rows whose foreign key points nowhere: (table, rowid, parent table, key index)."""
    async with conn.execute("PRAGMA foreign_key_check") as cur:
        return {(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()}


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("relaybay.db").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


async def migrate(conn: aiosqlite.Connection) -> int:
    """Apply numbered migrations newer than `PRAGMA user_version`; returns the resulting version."""
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0
    for number, sql in _migrations():
        if number <= current:
            continue
        # A table rebuild runs with foreign keys off (the pragma is a no-op inside a
        # transaction), and the result is checked before the commit.
        rebuild = sql.startswith(FOREIGN_KEYS_OFF)
        before: set[tuple[str, int, str, int]] = set()
        if rebuild:
            # Orphans an older database already had are not this migration's doing: they are
            # logged and kept; only violations the rebuild itself introduced stop it.
            before = await _broken_keys(conn)
            if before:
                tables = ", ".join(sorted({row[0] for row in before}))
                logger.warning(
                    "The database already has %d row(s) with a dangling foreign key (%s);"
                    " migration %04d keeps them.",
                    len(before),
                    tables,
                    number,
                )
            await conn.execute("PRAGMA foreign_keys=OFF")
        try:
            # One script so SQLite parses statements itself; the open transaction keeps it atomic.
            await conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};")
            if rebuild:
                new = await _broken_keys(conn) - before
                if new:
                    tables = ", ".join(sorted({row[0] for row in new}))
                    raise RuntimeError(
                        f"Migration {number:04d} would leave {len(new)} broken foreign key(s)"
                        f" in {tables}; nothing was changed."
                    )
            await conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise
        finally:
            if rebuild:
                await conn.execute("PRAGMA foreign_keys=ON")
        current = number
    return current
