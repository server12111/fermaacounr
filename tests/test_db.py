import tempfile
import unittest
from pathlib import Path

from db import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "farm.db"))
        await self.db.init()
        await self.db.ensure_chat("https://t.me/example", "Example")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_service_account_never_returns_to_pool(self):
        account_id = await self.db.add_account("+100000001", "A", "encrypted", tg_user_id=1)
        await self.db.set_account_role(account_id, "admin_controller")
        await self.db.assign_unassigned_accounts(45)
        account = await self.db.get_account(account_id)
        self.assertEqual(account.role, "admin_controller")
        self.assertIsNone(account.bonus_chat_id)

    async def test_chat_reference_normalization(self):
        first = await self.db.ensure_chat("https://t.me/MyPublicChat", "One")
        second = await self.db.ensure_chat("@mypublicchat", "Two")
        self.assertEqual(first, second)

    async def test_order_progress_is_persistent(self):
        account_id = await self.db.add_account("+100000002", "B", "encrypted", tg_user_id=2)
        order_id = await self.db.create_order(
            admin_chat_id=10, kind="views", link="https://t.me/example/1",
            reaction="auto", prerequisite_links=[], cooldown_seconds=0, requested_quantity=1,
        )
        await self.db.prepare_order(order_id, [account_id], 0)
        await self.db.set_order_item(order_id, account_id, "success", "ok")
        self.assertEqual(await self.db.finish_order(order_id), (1, 0))
        order = await self.db.get_order(order_id)
        self.assertEqual(order.status, "completed")

    async def test_delete_removes_session_but_keeps_order_aggregate(self):
        account_id = await self.db.add_account("+100000003", "C", "encrypted", tg_user_id=3)
        order_id = await self.db.create_order(
            admin_chat_id=10, kind="views", link="https://t.me/example/1",
            reaction="auto", prerequisite_links=[], cooldown_seconds=0, requested_quantity=1,
        )
        await self.db.prepare_order(order_id, [account_id], 0)
        await self.db.set_order_item(order_id, account_id, "success", "ok")
        await self.db.finish_order(order_id)
        self.assertTrue(await self.db.delete_account(account_id))
        self.assertIsNone(await self.db.get_account(account_id))
        self.assertIsNotNone(await self.db.get_order(order_id))
