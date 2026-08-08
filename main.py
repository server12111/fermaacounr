from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_config
from db import Database
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
    await db.reschedule(config.interval_seconds)
    cipher = SessionCipher(config.session_secret)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify_admins(account, status: str) -> None:
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, f"🌾 {account.display_name}: {status}")
            except Exception:
                logging.getLogger(__name__).exception("Не удалось уведомить администратора %s", admin_id)

    scheduler = FarmScheduler(db, config, cipher, on_result=notify_admins)
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
