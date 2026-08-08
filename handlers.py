from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from config import Config
from db import Account, Database
from farm import FarmScheduler, SessionCipher
from keyboards import (
    account_menu,
    accounts_menu,
    cancel_menu,
    code_keyboard,
    confirm_delete,
    main_menu,
)
from states import AddAccount


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and user.id in self.admin_ids:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("⛔ Доступ только для администраторов.")
        return None


class PendingLogin:
    def __init__(self) -> None:
        self.clients: dict[int, TelegramClient] = {}

    async def discard(self, admin_id: int) -> None:
        client = self.clients.pop(admin_id, None)
        if client:
            await client.disconnect()


def _format_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def account_text(account: Account) -> str:
    enabled = "включён" if account.enabled else "выключен"
    return (
        f"<b>{escape(account.display_name)}</b>\n"
        f"Телефон: <code>{escape(account.phone)}</code>\n"
        f"Автозапуск: {enabled}\n"
        f"Последний запуск: {_format_time(account.last_run)}\n"
        f"Следующий запуск: {_format_time(account.next_run)}\n"
        f"Результат: {escape(account.last_status or 'ещё не запускался')}"
    )


async def _edit(callback: CallbackQuery, text: str, markup) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


def build_router(
    config: Config,
    db: Database,
    scheduler: FarmScheduler,
    cipher: SessionCipher,
) -> Router:
    router = Router()
    router.message.outer_middleware(AdminOnlyMiddleware(config.admin_ids))
    router.callback_query.outer_middleware(AdminOnlyMiddleware(config.admin_ids))
    pending = PendingLogin()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await pending.discard(message.from_user.id)
        await message.answer("🌾 <b>Управление фермой</b>", reply_markup=main_menu())

    @router.callback_query(F.data == "home")
    async def home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await pending.discard(callback.from_user.id)
        await _edit(callback, "🌾 <b>Управление фермой</b>", main_menu())
        await callback.answer()

    @router.callback_query(F.data == "accounts")
    async def show_accounts(callback: CallbackQuery) -> None:
        accounts = await db.list_accounts()
        text = f"👥 <b>Аккаунты</b>\n\nДобавлено: {len(accounts)}"
        await _edit(callback, text, accounts_menu(accounts))
        await callback.answer()

    @router.callback_query(F.data.startswith("account:"))
    async def show_account(callback: CallbackQuery) -> None:
        account = await db.get_account(int(callback.data.split(":", 1)[1]))
        if not account:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await _edit(callback, account_text(account), account_menu(account))
        await callback.answer()

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await db.toggle_account(account_id)
        account = await db.get_account(account_id)
        if account:
            await _edit(callback, account_text(account), account_menu(account))
        await callback.answer("Состояние изменено")

    @router.callback_query(F.data.startswith("delete_ask:"))
    async def delete_ask(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await _edit(callback, "Удалить аккаунт и его сохранённую сессию?", confirm_delete(account_id))
        await callback.answer()

    @router.callback_query(F.data.startswith("delete:"))
    async def delete(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await db.delete_account(account_id)
        accounts = await db.list_accounts()
        await _edit(callback, "✅ Аккаунт удалён.", accounts_menu(accounts))
        await callback.answer()

    @router.callback_query(F.data.startswith("run:"))
    async def run_one(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await callback.answer("Запуск начат")
        if callback.message:
            await callback.message.answer("⏳ Выполняю команду…")

        async def worker() -> None:
            status = await scheduler.run_one(account_id)
            if callback.message:
                await callback.message.answer(status)

        asyncio.create_task(worker())

    @router.callback_query(F.data == "status")
    async def status(callback: CallbackQuery) -> None:
        accounts = await db.list_accounts()
        active = sum(account.enabled for account in accounts)
        successful = sum(bool(account.last_status and account.last_status.startswith("✅")) for account in accounts)
        text = (
            "📊 <b>Статус</b>\n\n"
            f"Всего аккаунтов: {len(accounts)}\n"
            f"Включено: {active}\n"
            f"Последний запуск успешен: {successful}"
        )
        await _edit(callback, text, main_menu())
        await callback.answer()

    @router.callback_query(F.data == "add_account")
    async def add_account(callback: CallbackQuery, state: FSMContext) -> None:
        await pending.discard(callback.from_user.id)
        await state.clear()
        await state.set_state(AddAccount.phone)
        await _edit(
            callback,
            "Введите номер аккаунта в международном формате, например <code>+380501234567</code>.",
            cancel_menu(),
        )
        await callback.answer()

    @router.callback_query(F.data == "cancel_add")
    async def cancel_add(callback: CallbackQuery, state: FSMContext) -> None:
        await pending.discard(callback.from_user.id)
        await state.clear()
        await _edit(callback, "Добавление отменено.", main_menu())
        await callback.answer()

    @router.message(AddAccount.phone)
    async def receive_phone(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").replace(" ", "").strip()
        if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
            await message.answer("Неверный формат номера. Пример: <code>+380501234567</code>", reply_markup=cancel_menu())
            return

        await pending.discard(message.from_user.id)
        client = TelegramClient(StringSession(), config.api_id, config.api_hash)
        try:
            await client.connect()
            await client.send_code_request(phone)
        except Exception as exc:
            await client.disconnect()
            await message.answer(f"❌ Не удалось запросить код: {escape(str(exc))}", reply_markup=cancel_menu())
            return
        pending.clients[message.from_user.id] = client
        await state.update_data(phone=phone, code="")
        await state.set_state(AddAccount.code)
        await message.answer("Код отправлен Telegram. Наберите его кнопками: <code>—</code>", reply_markup=code_keyboard())

    @router.callback_query(AddAccount.code, F.data.startswith("code:"))
    async def receive_code(callback: CallbackQuery, state: FSMContext) -> None:
        action = callback.data.split(":", 1)[1]
        data = await state.get_data()
        code = data.get("code", "")
        if action == "back":
            code = code[:-1]
        elif action.isdigit() and len(code) < 8:
            code += action
        elif action != "submit":
            await callback.answer()
            return
        await state.update_data(code=code)

        if action != "submit":
            shown = "•" * len(code) or "—"
            await _edit(callback, f"Код отправлен Telegram. Наберите его кнопками: <code>{shown}</code>", code_keyboard())
            await callback.answer()
            return
        if not code:
            await callback.answer("Сначала введите код", show_alert=True)
            return

        client = pending.clients.get(callback.from_user.id)
        if not client:
            await state.clear()
            await callback.answer("Авторизация устарела, начните заново", show_alert=True)
            return
        try:
            await client.sign_in(phone=data["phone"], code=code)
        except SessionPasswordNeededError:
            await state.set_state(AddAccount.password)
            await _edit(
                callback,
                "На аккаунте включён облачный пароль. Отправьте его сообщением. Сообщение будет сразу удалено, если у бота есть право удаления.",
                cancel_menu(),
            )
            await callback.answer()
            return
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await state.update_data(code="")
            await callback.answer("Код неверный или истёк", show_alert=True)
            await _edit(callback, "Введите новый код: <code>—</code>", code_keyboard())
            return
        await finish_login(callback.message, callback.from_user.id, state, pending, db, cipher)
        await callback.answer()

    @router.message(AddAccount.password)
    async def receive_password(message: Message, state: FSMContext) -> None:
        password = message.text or ""
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        client = pending.clients.get(message.from_user.id)
        if not client:
            await state.clear()
            await message.answer("Авторизация устарела. Начните заново.", reply_markup=main_menu())
            return
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError:
            await message.answer("❌ Неверный пароль. Попробуйте ещё раз.", reply_markup=cancel_menu())
            return
        await finish_login(message, message.from_user.id, state, pending, db, cipher)

    return router


async def finish_login(
    event_message: Message | None,
    admin_id: int,
    state: FSMContext,
    pending: PendingLogin,
    db: Database,
    cipher: SessionCipher,
) -> None:
    client = pending.clients.get(admin_id)
    if not client or not event_message:
        return
    data = await state.get_data()
    me = await client.get_me()
    display_name = " ".join(part for part in [me.first_name, me.last_name] if part) or str(me.id)
    session_string = client.session.save()
    try:
        await db.add_account(data["phone"], display_name, cipher.encrypt(session_string))
    except Exception as exc:
        await event_message.answer(f"❌ Не удалось сохранить аккаунт: {escape(str(exc))}", reply_markup=main_menu())
    else:
        await event_message.answer(
            f"✅ Аккаунт <b>{escape(display_name)}</b> добавлен. Первый запуск будет через 1 час.",
            reply_markup=main_menu(),
        )
    finally:
        await pending.discard(admin_id)
        await state.clear()
