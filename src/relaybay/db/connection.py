import re
from importlib import resources
from pathlib import Path

import aiosqlite

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


async def connect(path: Path) -> aiosqlite.Connection:
    # Autocommit mode: the service opens transactions explicitly with BEGIN IMMEDIATE.
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
        # One script so SQLite parses statements itself; BEGIN/COMMIT inside keeps it atomic.
        script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;"
        try:
            await conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise
        current = number
    return current
