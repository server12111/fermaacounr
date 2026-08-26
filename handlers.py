from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path
from urllib.parse import urlparse
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
from db import Account, ChatPoolFullError, Database
from farm import FarmScheduler, SessionCipher
from keyboards import (
    add_account_method_menu,
    account_menu,
    accounts_menu,
    chat_menu,
    chats_menu,
    cancel_menu,
    code_keyboard,
    confirm_delete,
    confirm_chat_delete,
    main_menu,
    boost_menu,
    reaction_groups_menu,
    reactions_menu,
    order_started_menu,
)
from states import AddAccount, AddChat, EngagementOrder
from tdata_import import extract_import_batch, session_to_string_session, tdata_to_string_session


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
        self.expiry_tasks: dict[int, asyncio.Task] = {}

    async def set(self, admin_id: int, client: TelegramClient) -> None:
        await self.discard(admin_id)
        self.clients[admin_id] = client

        async def expire() -> None:
            await asyncio.sleep(600)
            stale = self.clients.pop(admin_id, None)
            self.expiry_tasks.pop(admin_id, None)
            if stale:
                await stale.disconnect()

        self.expiry_tasks[admin_id] = asyncio.create_task(expire(), name=f"login-expiry-{admin_id}")

    async def discard(self, admin_id: int) -> None:
        task = self.expiry_tasks.pop(admin_id, None)
        current = asyncio.current_task()
        if task and task is not current:
            task.cancel()
        client = self.clients.pop(admin_id, None)
        if client:
            await client.disconnect()

    async def close_all(self) -> None:
        for admin_id in list(self.clients):
            await self.discard(admin_id)


def _format_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _validate_telegram_link(value: str, *, post: bool = False) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or (parsed.hostname or "").casefold() not in {
        "t.me", "telegram.me", "www.t.me", "www.telegram.me"
    }:
        raise ValueError("Нужна полная ссылка https://t.me/...")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("Telegram-ссылка пустая")
    check = parts[1:] if parts[0] == "s" else parts
    if post and (len(check) < 2 or not check[-1].isdigit()):
        raise ValueError("Нужна полная ссылка на конкретный пост")
    return value.strip()


def account_text(account: Account) -> str:
    state = "● работает" if account.enabled else "○ остановлен"
    role = "★ служебный админ-аккаунт" if account.role == "admin_controller" else "рабочий аккаунт"
    chat = escape(account.bonus_chat_title or "не назначен")
    if account.role == "admin_controller":
        chat = "управляет правами во всех чатах пула"
    result = escape(account.last_status or "запусков ещё не было")
    bonus = escape(account.bonus_status or "бонус ещё не запускался")
    stall = escape(account.stall_status or "ларёк ещё не запускался")
    return (
        f"<b>{escape(account.display_name)}</b>  {state}\n"
        f"Роль: <b>{role}</b>\n"
        f"<code>{escape(account.phone)}</code>\n\n"
        f"<b>Назначенный чат</b>\n{chat}\n\n"
        f"<b>Ферма</b>\nСледующий запуск: {_format_time(account.next_run)}\n{result}\n\n"
        f"<b>Бонус</b>\nСледующая проверка: {_format_time(account.bonus_next_run)}\n{bonus}"
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
    router.shutdown.register(pending.close_all)

    async def home_text() -> str:
        accounts = await db.list_accounts()
        chats = await db.list_chats()
        active = sum(account.enabled for account in accounts)
        used = sum(chat.account_count for chat in chats)
        capacity = sum(config.chat_account_limit for chat in chats if chat.enabled)
        return (
            "<b>Gram Farm</b>\n"
            "Управление аккаунтами и распределением нагрузки\n\n"
            f"<b>{active}</b> активных из {len(accounts)}  ·  "
            f"<b>{len(chats)}</b> чатов  ·  <b>{used}/{capacity}</b> мест"
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await pending.discard(message.from_user.id)
        await message.answer(await home_text(), reply_markup=main_menu())

    @router.callback_query(F.data == "home")
    async def home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await pending.discard(callback.from_user.id)
        await _edit(callback, await home_text(), main_menu())
        await callback.answer()

    @router.callback_query(F.data == "accounts")
    async def show_accounts(callback: CallbackQuery) -> None:
        accounts = await db.list_accounts()
        active = sum(account.enabled for account in accounts)
        text = (
            "<b>Аккаунты</b>\n"
            f"{active} активных из {len(accounts)}. Новый аккаунт получит самый свободный чат."
        )
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

    @router.callback_query(F.data.startswith("account_role_admin:"))
    async def make_admin_account(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        if await scheduler.is_account_running(account_id):
            await callback.answer("Аккаунт занят. Смените роль после завершения операции.", show_alert=True)
            return
        await db.set_account_role(account_id, "admin_controller", config.chat_account_limit)
        account = await db.get_account(account_id)
        if account:
            await _edit(callback, account_text(account), account_menu(account))
        await callback.answer("Аккаунт выделен как служебный админ", show_alert=True)

    @router.callback_query(F.data.startswith("account_role_worker:"))
    async def make_worker_account(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        if await scheduler.is_account_running(account_id):
            await callback.answer("Аккаунт занят. Смените роль после завершения операции.", show_alert=True)
            return
        await db.set_account_role(account_id, "worker", config.chat_account_limit)
        await db.assign_unassigned_accounts(config.chat_account_limit)
        account = await db.get_account(account_id)
        if account:
            await _edit(callback, account_text(account), account_menu(account))
        await callback.answer("Роль снята, аккаунт снова рабочий")

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        if await scheduler.is_account_running(account_id):
            await callback.answer("Аккаунт занят. Сначала дождитесь или отмените заказ.", show_alert=True)
            return
        if not await db.toggle_account(account_id):
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
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
        if await scheduler.is_account_running(account_id):
            await callback.answer("Аккаунт занят. Удаление сейчас небезопасно.", show_alert=True)
            return
        try:
            deleted = await db.delete_account(account_id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if not deleted:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        accounts = await db.list_accounts()
        await _edit(callback, "✅ Аккаунт удалён.", accounts_menu(accounts))
        await callback.answer()

    @router.callback_query(F.data.startswith("run:"))
    async def run_one(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await callback.answer("Запуск начат")
        if callback.message:
            await callback.message.answer("⏳ Выполняю команду…")

        scheduler.launch_manual(account_id)

    @router.callback_query(F.data.startswith("run_bonus:"))
    async def run_bonus(callback: CallbackQuery) -> None:
        account_id = int(callback.data.split(":", 1)[1])
        await callback.answer("Проверка баланса начата")
        if callback.message:
            state = "включена" if config.payout_enabled else "ВЫКЛЮЧЕНА"
            await callback.message.answer(
                f"⏳ Проверяю бонус и баланс. Автовыплата: <b>{state}</b>, "
                f"порог <b>{config.payout_threshold}</b>."
            )
        scheduler.launch_bonus_manual(account_id)

    @router.callback_query(F.data == "status")
    async def status(callback: CallbackQuery) -> None:
        accounts = await db.list_accounts()
        active = sum(account.enabled for account in accounts)
        successful = sum(bool(account.last_status and account.last_status.startswith("✅")) for account in accounts)
        chats = await db.list_chats()
        full = sum(chat.account_count >= config.chat_account_limit for chat in chats)
        unassigned = sum(account.role == "worker" and account.bonus_chat_id is None for account in accounts)
        text = (
            "<b>Обзор системы</b>\n"
            f"{active}/{len(accounts)} аккаунтов активны\n"
            f"{successful} последних запусков успешны\n\n"
            f"<b>Пул</b>\n{len(chats)} чатов, {full} заполнены\n"
            f"{unassigned} аккаунтов без назначения\n"
            f"Лимит: {config.chat_account_limit} аккаунтов на чат\n\n"
            f"<b>Автовыплата</b>\n"
            f"{'включена' if config.payout_enabled else 'ВЫКЛЮЧЕНА'} · "
            f"порог {config.payout_threshold} · максимум {config.payout_max_amount}"
        )
        await _edit(callback, text, main_menu())
        await callback.answer()

    async def ask_quantity(event: Message | CallbackQuery, state: FSMContext) -> None:
        maximum = await scheduler.available_order_count()
        text = (
            f"Введите количество аккаунтов. Максимально доступно сейчас: <b>{maximum}</b>."
            if maximum else "Сейчас нет свободных активных аккаунтов с сессиями."
        )
        if isinstance(event, CallbackQuery):
            await _edit(event, text, cancel_menu("boost"))
            await event.answer()
        else:
            await event.answer(text, reply_markup=cancel_menu("boost"))
        if maximum:
            await state.set_state(EngagementOrder.quantity)
        else:
            await state.clear()

    @router.callback_query(F.data == "boost")
    async def show_boost(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _edit(callback, "<b>Накрутка</b>\nВыберите тип заказа.", boost_menu())
        await callback.answer()

    @router.callback_query(F.data.startswith("boost:"))
    async def start_boost(callback: CallbackQuery, state: FSMContext) -> None:
        kind = callback.data.split(":", 1)[1]
        labels = {"reactions": "пост", "followers": "профиль, группу или канал",
                  "referral": "реферальную ссылку бота", "views": "пост"}
        if kind not in labels:
            await callback.answer("Неизвестный тип заказа", show_alert=True)
            return
        await state.clear()
        await state.update_data(kind=kind)
        await state.set_state(EngagementOrder.link)
        await _edit(callback, f"Отправьте ссылку на {labels[kind]}.", cancel_menu("boost"))
        await callback.answer()

    @router.message(EngagementOrder.link)
    async def receive_boost_link(message: Message, state: FSMContext) -> None:
        raw_link = (message.text or "").strip()
        kind = (await state.get_data())["kind"]
        try:
            link = _validate_telegram_link(raw_link, post=kind in {"reactions", "views"})
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=cancel_menu("boost"))
            return
        await state.update_data(link=link)
        if kind == "reactions":
            await state.set_state(EngagementOrder.reaction)
            await message.answer(
                "Какие реакции ставить? Если пропустить, будет выбрана самая используемая реакция поста.",
                reply_markup=reaction_groups_menu(),
            )
        elif kind == "followers":
            await state.set_state(EngagementOrder.cooldown)
            await message.answer("Введите задержку между подписками в минутах (можно 0).", reply_markup=cancel_menu("boost"))
        elif kind == "referral":
            await state.set_state(EngagementOrder.prerequisite_links)
            await message.answer(
                "Отправьте ссылки, куда аккаунты должны зайти до запуска бота. Каждая ссылка с новой строки. Если ссылок нет, отправьте «Пропустить».",
                reply_markup=cancel_menu("boost"),
            )
        else:
            await ask_quantity(message, state)

    @router.callback_query(EngagementOrder.reaction, F.data.startswith("reaction_group:"))
    async def choose_reaction_group(callback: CallbackQuery) -> None:
        await _edit(callback, "Выберите конкретную реакцию.", reactions_menu(callback.data.split(":", 1)[1]))
        await callback.answer()

    @router.callback_query(EngagementOrder.reaction, F.data == "reaction_back")
    async def reaction_back(callback: CallbackQuery) -> None:
        await _edit(callback, "Какие реакции ставить?", reaction_groups_menu())
        await callback.answer()

    @router.callback_query(EngagementOrder.reaction, F.data.startswith("reaction_pick:"))
    async def choose_reaction(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(reaction=callback.data.split(":", 1)[1])
        await ask_quantity(callback, state)

    @router.message(EngagementOrder.prerequisite_links)
    async def receive_prerequisite_links(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        links = [] if raw.casefold() == "пропустить" else [line.strip() for line in raw.splitlines() if line.strip()]
        try:
            links = [_validate_telegram_link(link) for link in links]
        except ValueError as exc:
            await message.answer(f"Ошибка в списке: {exc}")
            return
        await state.update_data(prerequisite_links=links)
        await state.set_state(EngagementOrder.cooldown)
        await message.answer("Введите задержку между переходами по ссылкам в минутах (можно 0).", reply_markup=cancel_menu("boost"))

    @router.message(EngagementOrder.cooldown)
    async def receive_cooldown(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip().replace(",", ".")
        try:
            minutes = float(raw)
        except ValueError:
            minutes = -1
        if not math.isfinite(minutes) or not 0 <= minutes <= 1440:
            await message.answer("Введите число от 0 до 1440 минут, например 2 или 0.5.")
            return
        await state.update_data(cooldown_seconds=int(minutes * 60))
        await ask_quantity(message, state)

    @router.message(EngagementOrder.quantity)
    async def receive_quantity(message: Message, state: FSMContext, bot: Bot) -> None:
        raw = (message.text or "").strip()
        maximum = await scheduler.available_order_count()
        if not raw.isdigit() or not 1 <= int(raw) <= maximum:
            await message.answer(f"Введите целое число от 1 до {maximum}." if maximum else "Свободных аккаунтов больше нет.", reply_markup=cancel_menu("boost"))
            return
        quantity = int(raw)
        data = await state.get_data()
        await state.clear()
        order_id = await db.create_order(
            admin_chat_id=message.chat.id,
            kind=data["kind"],
            link=data["link"],
            reaction=data.get("reaction", "auto"),
            prerequisite_links=data.get("prerequisite_links", []),
            cooldown_seconds=data.get("cooldown_seconds", 0),
            requested_quantity=quantity,
        )
        await message.answer(
            f"⏳ Заказ <b>#{order_id}</b> сохранён и запущен на <b>{quantity}</b> аккаунтах.",
            reply_markup=order_started_menu(order_id),
        )
        scheduler.launch_order(order_id)

    @router.callback_query(F.data.startswith("order_cancel:"))
    async def cancel_order(callback: CallbackQuery) -> None:
        order_id = int(callback.data.split(":", 1)[1])
        if await db.request_order_cancel(order_id):
            await callback.answer("Отмена запрошена. Текущее RPC будет завершено безопасно.", show_alert=True)
        else:
            await callback.answer("Заказ уже завершён или не найден", show_alert=True)

    @router.callback_query(F.data == "chats")
    async def show_chats(callback: CallbackQuery) -> None:
        chats = await db.list_chats()
        used = sum(chat.account_count for chat in chats)
        capacity = sum(config.chat_account_limit for chat in chats if chat.enabled)
        text = (
            "<b>Пул чатов</b>\n"
            f"{len(chats)} чатов, занято {used}/{capacity} мест. "
            f"Лимит каждого чата: {config.chat_account_limit}."
        )
        await _edit(callback, text, chats_menu(chats, config.chat_account_limit))
        await callback.answer()

    @router.callback_query(F.data.startswith("chat:"))
    async def show_chat(callback: CallbackQuery) -> None:
        chat = await db.get_chat(int(callback.data.split(":", 1)[1]))
        if not chat:
            await callback.answer("Чат не найден", show_alert=True)
            return
        state = "● участвует в распределении" if chat.enabled else "○ новые аккаунты не назначаются"
        text = (
            f"<b>{escape(chat.title)}</b>\n{state}\n\n"
            f"Занято: <b>{chat.account_count}/{config.chat_account_limit}</b>\n"
            f"Адрес: <code>{escape(chat.reference)}</code>"
        )
        await _edit(callback, text, chat_menu(chat))
        await callback.answer()

    @router.callback_query(F.data.startswith("chat_toggle:"))
    async def toggle_chat(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 1)[1])
        await db.toggle_chat(chat_id)
        chat = await db.get_chat(chat_id)
        if chat:
            state = "● участвует в распределении" if chat.enabled else "○ новые аккаунты не назначаются"
            text = (
                f"<b>{escape(chat.title)}</b>\n{state}\n\n"
                f"Занято: <b>{chat.account_count}/{config.chat_account_limit}</b>\n"
                f"Адрес: <code>{escape(chat.reference)}</code>"
            )
            await _edit(callback, text, chat_menu(chat))
        await callback.answer("Настройка сохранена")

    @router.callback_query(F.data.startswith("chat_delete_ask:"))
    async def delete_chat_ask(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 1)[1])
        chat = await db.get_chat(chat_id)
        if not chat:
            await callback.answer("Чат не найден", show_alert=True)
            return
        await _edit(
            callback,
            f"<b>Удалить {escape(chat.title)}?</b>\n"
            f"{chat.account_count} аккаунтов будут распределены по другим чатам.",
            confirm_chat_delete(chat_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("chat_delete:"))
    async def delete_chat(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 1)[1])
        try:
            await db.delete_chat(chat_id, config.chat_account_limit)
        except ChatPoolFullError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        chats = await db.list_chats()
        await _edit(callback, "<b>Чат удалён</b>\nАккаунты перераспределены.", chats_menu(chats, config.chat_account_limit))
        await callback.answer()

    @router.callback_query(F.data == "add_chat")
    async def add_chat(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddChat.reference)
        await _edit(
            callback,
            "<b>Новый чат</b>\nОтправьте <code>Название | ссылка</code> или просто @username. "
            "Поддерживаются публичные и приватные ссылки-приглашения.",
            cancel_menu("cancel_chat"),
        )
        await callback.answer()

    @router.callback_query(F.data == "cancel_chat")
    async def cancel_chat(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        chats = await db.list_chats()
        await _edit(callback, "<b>Пул чатов</b>", chats_menu(chats, config.chat_account_limit))
        await callback.answer()

    @router.message(AddChat.reference)
    async def receive_chat(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if "|" in raw:
            title, reference = (part.strip() for part in raw.split("|", 1))
        else:
            reference = raw
            title = reference.replace("https://t.me/", "@").strip("/")
        if not reference or not (reference.startswith("@") or "t.me/" in reference):
            await message.answer("Нужен @username или ссылка t.me. Попробуйте ещё раз.", reply_markup=cancel_menu("cancel_chat"))
            return
        await db.ensure_chat(reference, title or reference)
        assigned = await db.assign_unassigned_accounts(config.chat_account_limit)
        await state.clear()
        chats = await db.list_chats()
        suffix = f" Сразу назначено аккаунтов: {assigned}." if assigned else ""
        await message.answer(
            f"<b>Чат добавлен в пул.</b>{suffix}",
            reply_markup=chats_menu(chats, config.chat_account_limit),
        )

    @router.callback_query(F.data == "add_account")
    async def add_account(callback: CallbackQuery, state: FSMContext) -> None:
        await pending.discard(callback.from_user.id)
        await state.clear()
        await state.set_state(AddAccount.method)
        await _edit(
            callback,
            "<b>Добавление аккаунта</b>\nВыберите способ авторизации.",
            add_account_method_menu(),
        )
        await callback.answer()

    @router.callback_query(AddAccount.method, F.data == "add_account_phone")
    async def add_account_phone(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddAccount.phone)
        await _edit(
            callback,
            "Введите номер аккаунта в международном формате, например <code>+380501234567</code>.",
            cancel_menu(),
        )
        await callback.answer()

    @router.callback_query(AddAccount.method, F.data == "add_account_tdata")
    async def add_account_tdata(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddAccount.tdata)
        await _edit(
            callback,
            "<b>Импорт аккаунтов</b>\nОтправьте .session или общий ZIP с несколькими "
            "файлами <code>.session</code> / <code>tdata.zip</code>. Бот импортирует их по очереди. "
            "Перед экспортом отключите локальный пароль Telegram Desktop. "
            "Временные файлы удаляются сразу после импорта.",
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
        await pending.set(message.from_user.id, client)
        await state.update_data(phone=phone, code="")
        await state.set_state(AddAccount.code)
        await message.answer("Код отправлен Telegram. Наберите его кнопками: <code>—</code>", reply_markup=code_keyboard())

    @router.message(AddAccount.tdata)
    async def receive_tdata(message: Message, state: FSMContext, bot: Bot) -> None:
        document = message.document
        filename = (document.file_name or "") if document else ""
        is_zip = filename.casefold().endswith(".zip")
        is_session = filename.casefold().endswith(".session")
        if not document or not (is_zip or is_session):
            await message.answer(
                "Нужен файл .session или ZIP с несколькими .session/tdata.zip.",
                reply_markup=cancel_menu(),
            )
            return
        if document.file_size and document.file_size > 50 * 1024 * 1024:
            await message.answer(
                "Архив больше 50 МБ. Уменьшите его и попробуйте снова.",
                reply_markup=cancel_menu(),
            )
            return

        status = await message.answer("⏳ Проверяю архив и ищу аккаунты…")
        imported: list[str] = []
        errors: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="gram-farm-tdata-") as tmp:
                root = Path(tmp)
                archive = root / ("accounts.zip" if is_zip else filename)
                await bot.download(document, destination=archive)
                if is_zip:
                    packages = extract_import_batch(archive, root / "unpacked")
                else:
                    packages = [(archive.stem, "session", archive)]

                for index, (source_name, package_kind, package_path) in enumerate(packages, start=1):
                    await status.edit_text(
                        f"⏳ Импортирую аккаунт {index}/{len(packages)}…"
                    )
                    try:
                        if package_kind == "tdata":
                            session_string, me = await tdata_to_string_session(package_path)
                        else:
                            session_string, me = await session_to_string_session(
                                package_path, config.api_id, config.api_hash
                            )
                        display_name = " ".join(
                            part for part in [me.first_name, me.last_name] if part
                        ) or str(me.id)
                        phone = f"+{me.phone}" if getattr(me, "phone", None) else f"tg:{me.id}"
                        await db.add_account(
                            phone,
                            display_name,
                            cipher.encrypt(session_string),
                            config.chat_account_limit,
                            tg_user_id=me.id,
                        )
                        imported.append(display_name)
                    except Exception as exc:
                        errors.append(f"{source_name}: {str(exc)[:160]}")
        except Exception as exc:
            await status.edit_text(
                f"❌ Не удалось обработать архив: {escape(str(exc))}",
                reply_markup=cancel_menu(),
            )
            return

        await state.clear()
        summary = f"✅ Добавлено аккаунтов: <b>{len(imported)}</b>."
        if errors:
            preview = "\n".join(f"• {escape(item)}" for item in errors[:10])
            more = f"\n…и ещё {len(errors) - 10}" if len(errors) > 10 else ""
            summary += f"\nОшибок/дубликатов: <b>{len(errors)}</b>.\n{preview}{more}"
        summary += "\nПервый запуск новых аккаунтов будет через 1 час."
        await status.edit_text(summary, reply_markup=main_menu())

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
        await finish_login(callback.message, callback.from_user.id, state, pending, db, cipher, config.chat_account_limit)
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
        await finish_login(message, message.from_user.id, state, pending, db, cipher, config.chat_account_limit)

    return router


async def finish_login(
    event_message: Message | None,
    admin_id: int,
    state: FSMContext,
    pending: PendingLogin,
    db: Database,
    cipher: SessionCipher,
    chat_limit: int,
) -> None:
    client = pending.clients.get(admin_id)
    if not client or not event_message:
        return
    data = await state.get_data()
    me = await client.get_me()
    display_name = " ".join(part for part in [me.first_name, me.last_name] if part) or str(me.id)
    session_string = client.session.save()
    try:
        await db.add_account(
            data["phone"], display_name, cipher.encrypt(session_string), chat_limit, tg_user_id=me.id
        )
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
