from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite


@dataclass(slots=True)
class Account:
    id: int
    phone: str
    display_name: str
    session: str
    enabled: bool
    last_run: str | None
    next_run: str | None
    last_status: str | None
    available_from: str | None
    bonus_next_run: str | None
    bonus_status: str | None
    stall_next_run: str | None
    stall_status: str | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    session TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run TEXT,
                    next_run TEXT,
                    last_status TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in await (await conn.execute("PRAGMA table_info(accounts)")).fetchall()
            }
            if "available_from" not in columns:
                await conn.execute("ALTER TABLE accounts ADD COLUMN available_from TEXT")
            if "bonus_next_run" not in columns:
                await conn.execute("ALTER TABLE accounts ADD COLUMN bonus_next_run TEXT")
            if "bonus_status" not in columns:
                await conn.execute("ALTER TABLE accounts ADD COLUMN bonus_status TEXT")
            if "stall_next_run" not in columns:
                await conn.execute("ALTER TABLE accounts ADD COLUMN stall_next_run TEXT")
            if "stall_status" not in columns:
                await conn.execute("ALTER TABLE accounts ADD COLUMN stall_status TEXT")
            await conn.commit()

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> Account | None:
        return Account(**dict(row)) if row else None

    async def list_accounts(self, enabled_only: bool = False) -> list[Account]:
        query = "SELECT id, phone, display_name, session, enabled, last_run, next_run, last_status, available_from, bonus_next_run, bonus_status, stall_next_run, stall_status FROM accounts"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (await conn.execute(query)).fetchall()
        return [self._row(row) for row in rows]  # type: ignore[misc]

    async def get_account(self, account_id: int) -> Account | None:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            row = await (
                await conn.execute(
                    "SELECT id, phone, display_name, session, enabled, last_run, next_run, last_status, available_from, bonus_next_run, bonus_status, stall_next_run, stall_status "
                    "FROM accounts WHERE id = ?",
                    (account_id,),
                )
            ).fetchone()
        return self._row(row)

    async def add_account(self, phone: str, display_name: str, encrypted_session: str) -> int:
        now = datetime.now(UTC).isoformat()
        available_from = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        async with aiosqlite.connect(self.path) as conn:
            cursor = await conn.execute(
                "INSERT INTO accounts(phone, display_name, session, created_at, available_from, bonus_next_run, stall_next_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (phone, display_name, encrypted_session, now, available_from, available_from, available_from),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def toggle_account(self, account_id: int) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE accounts SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?",
                (account_id,),
            )
            await conn.commit()

    async def delete_account(self, account_id: int) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            await conn.commit()

    async def set_result(
        self, account_id: int, *, last_run: datetime, next_run: datetime, status: str
    ) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE accounts SET last_run = ?, next_run = ?, last_status = ? WHERE id = ?",
                (last_run.isoformat(), next_run.isoformat(), status[:500], account_id),
            )
            await conn.commit()

    async def quarantine(self, account_id: int, status: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE accounts SET enabled = 0, next_run = NULL, bonus_next_run = NULL, stall_next_run = NULL, last_status = ?, bonus_status = ?, stall_status = ? WHERE id = ?",
                (status[:500], status[:500], status[:500], account_id),
            )
            await conn.commit()

    async def set_bonus_result(self, account_id: int, *, next_run: datetime, status: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE accounts SET bonus_next_run = ?, bonus_status = ? WHERE id = ?",
                (next_run.isoformat(), status[:500], account_id),
            )
            await conn.commit()

    async def set_stall_result(self, account_id: int, *, next_run: datetime, status: str) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute(
                "UPDATE accounts SET stall_next_run = ?, stall_status = ? WHERE id = ?",
                (next_run.isoformat(), status[:500], account_id),
            )
            await conn.commit()

    async def reschedule(self, interval_seconds: int) -> None:
        """Пересчитать сроки после изменения интервала в конфигурации."""
        async with aiosqlite.connect(self.path) as conn:
            rows = await (await conn.execute("SELECT id, last_run FROM accounts WHERE last_run IS NOT NULL")).fetchall()
            for account_id, last_run in rows:
                next_run = datetime.fromisoformat(last_run) + timedelta(seconds=interval_seconds)
                await conn.execute(
                    "UPDATE accounts SET next_run = ? WHERE id = ?",
                    (next_run.isoformat(), account_id),
                )
            await conn.commit()
