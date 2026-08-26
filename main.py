from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_config
from db import Database, Order
from farm import FarmScheduler, SessionCipher
from handlers import build_router


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    config = load_config()
    db = Database(config.database_path)
    await db.init()
    await db.ensure_chat(config.bonus_chat, "Основной чат")
    assigned = await db.assign_unassigned_accounts(config.chat_account_limit)
    if assigned:
        logging.info("Назначено существующих аккаунтов в пул: %s", assigned)
    await db.reschedule(config.interval_seconds)
    cipher = SessionCipher(config.session_secret)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify_admins(account, status: str) -> None:
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, f"🌾 {escape(account.display_name)}: {escape(status)}")
            except Exception:
                logging.getLogger(__name__).exception("Не удалось уведомить администратора %s", admin_id)

    async def notify_order(order: Order, completed: int, errors: int) -> None:
        label = "отменён" if order.status == "cancelled" else "выполнен"
        text = (
            f"Заказ <b>#{order.id}</b> {label}. "
            f"Получилось сделать {completed}, ошибок — {errors}."
        )
        for attempt in range(3):
            try:
                await bot.send_message(order.admin_chat_id, text)
                return
            except Exception:
                if attempt == 2:
                    logging.getLogger(__name__).exception("Не удалось отправить итог заказа %s", order.id)
                else:
                    await asyncio.sleep(2 ** attempt)

    scheduler = FarmScheduler(
        db, config, cipher, on_result=notify_admins, on_order_result=notify_order
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(config, db, scheduler, cipher))

    scheduler.start()
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await scheduler.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
