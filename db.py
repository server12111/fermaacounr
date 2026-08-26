from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

import aiosqlite


@dataclass(slots=True)
class Account:
    id: int
    phone: str
    display_name: str
    tg_user_id: int | None
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
    bonus_chat_id: int | None
    bonus_chat_reference: str | None
    bonus_chat_title: str | None
    role: str


@dataclass(slots=True)
class Chat:
    id: int
    reference: str
    title: str
    enabled: bool
    account_count: int


@dataclass(slots=True)
class Order:
    id: int
    admin_chat_id: int
    kind: str
    link: str
    reaction: str
    prerequisite_links: list[str]
    cooldown_seconds: int
    requested_quantity: int
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    completed: int
    errors: int


class ChatPoolFullError(RuntimeError):
    pass


class Database:
    ACCOUNT_COLUMNS = (
        "a.id, a.phone, a.display_name, a.tg_user_id, a.session, a.enabled, a.last_run, "
        "a.next_run, a.last_status, a.available_from, a.bonus_next_run, "
        "a.bonus_status, a.stall_next_run, a.stall_status, a.bonus_chat_id, "
        "c.reference AS bonus_chat_reference, c.title AS bonus_chat_title, a.role"
    )

    def __init__(self, path: str) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self, *, rows: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path, timeout=30)
        try:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA busy_timeout = 30000")
            if rows:
                conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as conn:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    tg_user_id INTEGER,
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
            migrations = {
                "tg_user_id": "INTEGER",
                "available_from": "TEXT",
                "bonus_next_run": "TEXT",
                "bonus_status": "TEXT",
                "stall_next_run": "TEXT",
                "stall_status": "TEXT",
                "bonus_chat_id": "INTEGER REFERENCES chats(id) ON DELETE SET NULL",
                "role": "TEXT NOT NULL DEFAULT 'worker'",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    await conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('reactions','followers','referral','views')),
                    link TEXT NOT NULL,
                    reaction TEXT NOT NULL DEFAULT 'auto',
                    prerequisite_links TEXT NOT NULL DEFAULT '[]',
                    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                    requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued','running','cancelling','cancelled','completed','failed')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    completed INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_items (
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','success','error','cancelled')),
                    detail TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(order_id, account_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payout_ledger (
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, source_chat_id, source_message_id)
                )
                """
            )
            rows = await (await conn.execute(
                "SELECT id, created_at FROM accounts WHERE available_from IS NULL"
            )).fetchall()
            for account_id, created_at in rows:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                await conn.execute(
                    "UPDATE accounts SET available_from = ? WHERE id = ?",
                    ((created + timedelta(hours=1)).isoformat(), account_id),
                )
            await conn.execute("UPDATE accounts SET role = 'worker' WHERE role IS NULL OR role = ''")
            # A service account must never occupy a pool slot.
            await conn.execute("UPDATE accounts SET bonus_chat_id = NULL WHERE role = 'admin_controller'")
            # A process may have stopped between the RPC and result persistence. Never blindly repeat it.
            now = datetime.now(UTC).isoformat()
            await conn.execute(
                "UPDATE order_items SET status='error', detail='Прервано перезапуском; повтор не выполнен', updated_at=? WHERE status='running'",
                (now,),
            )
            await conn.execute("UPDATE orders SET status='queued' WHERE status='running'")
            await conn.commit()

    @staticmethod
    def _account(row: aiosqlite.Row | None) -> Account | None:
        return Account(**dict(row)) if row else None

    @staticmethod
    def _chat(row: aiosqlite.Row | None) -> Chat | None:
        return Chat(**dict(row)) if row else None

    @staticmethod
    def _order(row: aiosqlite.Row | None) -> Order | None:
        if not row:
            return None
        data = dict(row)
        data["prerequisite_links"] = json.loads(data["prerequisite_links"] or "[]")
        return Order(**data)

    @staticmethod
    def normalize_chat_reference(reference: str) -> str:
        value = reference.strip()
        if value.startswith("@"):
            return "@" + value[1:].casefold()
        parsed = urlparse(value)
        if (parsed.hostname or "").casefold() in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 1 and not parts[0].startswith("+"):
                return "@" + parts[0].casefold()
            if parts and parts[0].startswith("+"):
                return "https://t.me/" + parts[0]
            if len(parts) == 2 and parts[0] == "joinchat":
                return "https://t.me/+" + parts[1]
        return value

    async def ensure_chat(self, reference: str, title: str | None = None) -> int:
        reference = self.normalize_chat_reference(reference)
        if not reference:
            raise ValueError("Ссылка или @username чата не может быть пустой")
        async with self.connect() as conn:
            await conn.execute(
                """INSERT INTO chats(reference, title, created_at) VALUES (?, ?, ?)
                ON CONFLICT(reference) DO NOTHING""",
                (reference, (title or reference).strip(), datetime.now(UTC).isoformat()),
            )
            row = await (await conn.execute("SELECT id FROM chats WHERE reference = ?", (reference,))).fetchone()
            await conn.commit()
            return int(row[0])

    async def list_chats(self) -> list[Chat]:
        async with self.connect(rows=True) as conn:
            rows = await (await conn.execute(
                """SELECT c.id, c.reference, c.title, c.enabled,
                COUNT(CASE WHEN a.role='worker' THEN 1 END) AS account_count
                FROM chats c LEFT JOIN accounts a ON a.bonus_chat_id=c.id
                GROUP BY c.id ORDER BY c.enabled DESC, c.id"""
            )).fetchall()
        return [self._chat(row) for row in rows]  # type: ignore[misc]

    async def get_chat(self, chat_id: int) -> Chat | None:
        async with self.connect(rows=True) as conn:
            row = await (await conn.execute(
                """SELECT c.id, c.reference, c.title, c.enabled,
                COUNT(CASE WHEN a.role='worker' THEN 1 END) AS account_count
                FROM chats c LEFT JOIN accounts a ON a.bonus_chat_id=c.id
                WHERE c.id=? GROUP BY c.id""", (chat_id,)
            )).fetchone()
        return self._chat(row)

    async def toggle_chat(self, chat_id: int) -> bool:
        async with self.connect() as conn:
            cursor = await conn.execute(
                "UPDATE chats SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (chat_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_chat(self, chat_id: int, limit: int) -> None:
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            exists = await (await conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,))).fetchone()
            if not exists:
                await conn.rollback()
                raise ValueError("Чат не найден")
            assigned = await (await conn.execute(
                "SELECT id FROM accounts WHERE bonus_chat_id=? AND role='worker' ORDER BY id", (chat_id,)
            )).fetchall()
            for (account_id,) in assigned:
                replacement = await (await conn.execute(
                    """SELECT c.id FROM chats c LEFT JOIN accounts a
                    ON a.bonus_chat_id=c.id AND a.role='worker'
                    WHERE c.enabled=1 AND c.id!=? GROUP BY c.id HAVING COUNT(a.id)<?
                    ORDER BY COUNT(a.id), c.id LIMIT 1""", (chat_id, limit)
                )).fetchone()
                if not replacement:
                    await conn.rollback()
                    raise ChatPoolFullError("Не хватает мест в остальных чатах. Сначала добавьте чат в пул.")
                await conn.execute("UPDATE accounts SET bonus_chat_id=? WHERE id=?", (replacement[0], account_id))
            await conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            await conn.commit()

    async def assign_unassigned_accounts(self, limit: int) -> int:
        assigned = 0
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            rows = await (await conn.execute(
                "SELECT id FROM accounts WHERE bonus_chat_id IS NULL AND role='worker' ORDER BY id"
            )).fetchall()
            for (account_id,) in rows:
                chat = await (await conn.execute(
                    """SELECT c.id FROM chats c LEFT JOIN accounts a
                    ON a.bonus_chat_id=c.id AND a.role='worker'
                    WHERE c.enabled=1 GROUP BY c.id HAVING COUNT(a.id)<?
                    ORDER BY COUNT(a.id), c.id LIMIT 1""", (limit,)
                )).fetchone()
                if not chat:
                    break
                await conn.execute("UPDATE accounts SET bonus_chat_id=? WHERE id=?", (chat[0], account_id))
                assigned += 1
            await conn.commit()
        return assigned

    async def set_account_role(self, account_id: int, role: str, chat_limit: int = 45) -> None:
        if role not in {"worker", "admin_controller"}:
            raise ValueError("неизвестная роль аккаунта")
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            exists = await (await conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,))).fetchone()
            if not exists:
                await conn.rollback()
                raise ValueError("Аккаунт не найден")
            if role == "admin_controller":
                await conn.execute("UPDATE accounts SET role='worker' WHERE role='admin_controller'")
                await conn.execute("UPDATE accounts SET role=?, bonus_chat_id=NULL WHERE id=?", (role, account_id))
            else:
                await conn.execute("UPDATE accounts SET role=? WHERE id=?", (role, account_id))
            await conn.commit()
        await self.assign_unassigned_accounts(chat_limit)

    async def get_admin_controller(self) -> Account | None:
        async with self.connect(rows=True) as conn:
            row = await (await conn.execute(
                f"SELECT {self.ACCOUNT_COLUMNS} FROM accounts a LEFT JOIN chats c ON c.id=a.bonus_chat_id "
                "WHERE a.role='admin_controller' AND a.enabled=1 LIMIT 1"
            )).fetchone()
        return self._account(row)

    async def list_accounts(self, enabled_only: bool = False) -> list[Account]:
        query = f"SELECT {self.ACCOUNT_COLUMNS} FROM accounts a LEFT JOIN chats c ON c.id=a.bonus_chat_id"
        if enabled_only:
            query += " WHERE a.enabled=1"
        query += " ORDER BY a.id"
        async with self.connect(rows=True) as conn:
            rows = await (await conn.execute(query)).fetchall()
        return [self._account(row) for row in rows]  # type: ignore[misc]

    async def get_account(self, account_id: int) -> Account | None:
        async with self.connect(rows=True) as conn:
            row = await (await conn.execute(
                f"SELECT {self.ACCOUNT_COLUMNS} FROM accounts a LEFT JOIN chats c ON c.id=a.bonus_chat_id WHERE a.id=?",
                (account_id,),
            )).fetchone()
        return self._account(row)

    async def add_account(
        self, phone: str, display_name: str, encrypted_session: str,
        chat_limit: int = 45, tg_user_id: int | None = None,
    ) -> int:
        now = datetime.now(UTC)
        available_from = (now + timedelta(hours=1)).isoformat()
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            chat = await (await conn.execute(
                """SELECT c.id FROM chats c LEFT JOIN accounts a
                ON a.bonus_chat_id=c.id AND a.role='worker'
                WHERE c.enabled=1 GROUP BY c.id HAVING COUNT(a.id)<?
                ORDER BY COUNT(a.id), c.id LIMIT 1""", (chat_limit,)
            )).fetchone()
            cursor = await conn.execute(
                """INSERT INTO accounts(phone,display_name,tg_user_id,session,created_at,available_from,
                bonus_next_run,stall_next_run,bonus_chat_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                (phone, display_name, tg_user_id, encrypted_session, now.isoformat(), available_from,
                 available_from, available_from, chat[0] if chat else None),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def toggle_account(self, account_id: int) -> bool:
        async with self.connect() as conn:
            cursor = await conn.execute(
                "UPDATE accounts SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (account_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_account(self, account_id: int) -> bool:
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            # Completed order aggregates stay in orders; remove the account-level link so
            # deleting a session really removes its identity and encrypted credentials.
            await conn.execute("DELETE FROM order_items WHERE account_id=?", (account_id,))
            cursor = await conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def set_result(self, account_id: int, *, last_run: datetime, next_run: datetime, status: str) -> None:
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE accounts SET last_run=?,next_run=?,last_status=? WHERE id=?",
                (last_run.isoformat(), next_run.isoformat(), status[:500], account_id),
            )
            await conn.commit()

    async def quarantine(self, account_id: int, status: str) -> None:
        async with self.connect() as conn:
            await conn.execute(
                """UPDATE accounts SET enabled=0,next_run=NULL,bonus_next_run=NULL,stall_next_run=NULL,
                last_status=?,bonus_status=?,stall_status=? WHERE id=?""",
                (status[:500], status[:500], status[:500], account_id),
            )
            await conn.commit()

    async def set_bonus_result(self, account_id: int, *, next_run: datetime, status: str) -> None:
        async with self.connect() as conn:
            await conn.execute("UPDATE accounts SET bonus_next_run=?,bonus_status=? WHERE id=?",
                               (next_run.isoformat(), status[:500], account_id))
            await conn.commit()

    async def set_stall_result(self, account_id: int, *, next_run: datetime, status: str) -> None:
        async with self.connect() as conn:
            await conn.execute("UPDATE accounts SET stall_next_run=?,stall_status=? WHERE id=?",
                               (next_run.isoformat(), status[:500], account_id))
            await conn.commit()

    async def reschedule(self, interval_seconds: int) -> None:
        async with self.connect() as conn:
            rows = await (await conn.execute("SELECT id,last_run FROM accounts WHERE last_run IS NOT NULL")).fetchall()
            for account_id, last_run in rows:
                next_run = datetime.fromisoformat(last_run) + timedelta(seconds=interval_seconds)
                await conn.execute("UPDATE accounts SET next_run=? WHERE id=?", (next_run.isoformat(), account_id))
            await conn.commit()

    async def create_order(
        self, *, admin_chat_id: int, kind: str, link: str, reaction: str,
        prerequisite_links: list[str], cooldown_seconds: int, requested_quantity: int,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as conn:
            cursor = await conn.execute(
                """INSERT INTO orders(admin_chat_id,kind,link,reaction,prerequisite_links,
                cooldown_seconds,requested_quantity,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (admin_chat_id, kind, link, reaction, json.dumps(prerequisite_links, ensure_ascii=False),
                 cooldown_seconds, requested_quantity, now),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def get_order(self, order_id: int) -> Order | None:
        async with self.connect(rows=True) as conn:
            row = await (await conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
        return self._order(row)

    async def list_recoverable_orders(self) -> list[Order]:
        async with self.connect(rows=True) as conn:
            rows = await (await conn.execute(
                "SELECT * FROM orders WHERE status IN ('queued','running','cancelling') ORDER BY id"
            )).fetchall()
        return [self._order(row) for row in rows]  # type: ignore[misc]

    async def prepare_order(self, order_id: int, account_ids: list[int], missing: int) -> None:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("UPDATE orders SET status='running',started_at=COALESCE(started_at,?),errors=? WHERE id=?",
                               (now, missing, order_id))
            for account_id in account_ids:
                await conn.execute(
                    "INSERT OR IGNORE INTO order_items(order_id,account_id,status,updated_at) VALUES (?,?,'pending',?)",
                    (order_id, account_id, now),
                )
            await conn.commit()

    async def list_order_items(self, order_id: int, statuses: tuple[str, ...] = ("pending",)) -> list[int]:
        placeholders = ",".join("?" for _ in statuses)
        async with self.connect() as conn:
            rows = await (await conn.execute(
                f"SELECT account_id FROM order_items WHERE order_id=? AND status IN ({placeholders}) ORDER BY account_id",
                (order_id, *statuses),
            )).fetchall()
        return [int(row[0]) for row in rows]

    async def set_order_item(self, order_id: int, account_id: int, status: str, detail: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE order_items SET status=?,detail=?,updated_at=? WHERE order_id=? AND account_id=?",
                (status, detail[:1000], now, order_id, account_id),
            )
            await conn.commit()

    async def finish_order(self, order_id: int, status: str = "completed") -> tuple[int, int]:
        async with self.connect() as conn:
            row = await (await conn.execute(
                """SELECT SUM(status='success'), SUM(status IN ('error','cancelled'))
                FROM order_items WHERE order_id=?""", (order_id,)
            )).fetchone()
            order = await (await conn.execute("SELECT requested_quantity FROM orders WHERE id=?", (order_id,))).fetchone()
            completed = int(row[0] or 0)
            item_errors = int(row[1] or 0)
            total_items = completed + item_errors
            errors = item_errors + max(0, int(order[0]) - total_items)
            await conn.execute(
                "UPDATE orders SET status=?,finished_at=?,completed=?,errors=? WHERE id=?",
                (status, datetime.now(UTC).isoformat(), completed, errors, order_id),
            )
            await conn.commit()
        return completed, errors

    async def request_order_cancel(self, order_id: int) -> bool:
        async with self.connect() as conn:
            cursor = await conn.execute(
                "UPDATE orders SET status='cancelling' WHERE id=? AND status IN ('queued','running')", (order_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def claim_payout(self, account_id: int, chat_id: int, message_id: int, amount: int) -> bool:
        async with self.connect() as conn:
            cursor = await conn.execute(
                """INSERT OR IGNORE INTO payout_ledger(account_id,source_chat_id,source_message_id,amount,status,created_at)
                VALUES (?,?,?,?,'claimed',?)""",
                (account_id, chat_id, message_id, amount, datetime.now(UTC).isoformat()),
            )
            await conn.commit()
            return cursor.rowcount > 0
