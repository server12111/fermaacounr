from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon import functions
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
)
from telethon.sessions import StringSession

from config import Config
from db import Account, Database


class SafetyBlockError(RuntimeError):
    """Telegram ограничил аккаунт; автоматическое возобновление запрещено."""

    def __init__(self, label: str, message: str, seconds: int | None = None) -> None:
        super().__init__(message)
        self.label = label
        self.seconds = seconds


def _safety_status(exc: SafetyBlockError, now: datetime) -> str:
    if exc.seconds is not None:
        until = (now + timedelta(seconds=exc.seconds)).astimezone().strftime("%d.%m.%Y %H:%M:%S")
        return f"⛔ {exc.label}: {exc} До: {until}. Аккаунт отключён."
    return f"⛔ {exc.label}: {exc}. Аккаунт отключён."


def _timer_seconds(text: str) -> int | None:
    clock = re.search(r"(?<!\d)(\d{1,3}):(\d{2})(?!\d)", text)
    if clock:
        return int(clock.group(1)) * 3600 + int(clock.group(2)) * 60
    hours = re.search(r"(\d+)\s*ч", text.casefold())
    minutes = re.search(r"(\d+)\s*мин", text.casefold())
    if not hours and not minutes:
        return None
    total = 0
    if hours:
        total += int(hours.group(1)) * 3600
    if minutes:
        total += int(minutes.group(1)) * 60
    return total


async def _click_bonus_button(client: TelegramClient, message, row_index: int, button_index: int):
    button = message.buttons[row_index][button_index]
    url = getattr(button, "url", None)
    if not url:
        return await message.click(row_index, button_index)

    # URL-кнопки t.me не вызывают callback при message.click(). Эмулируем
    # переход по deep-link через StartBotRequest, чтобы бонусный бот получил
    # тот же параметр, что и при обычном нажатии пользователем.
    parsed = urlparse(url)
    if parsed.netloc.casefold() in {"t.me", "telegram.me"}:
        username = parsed.path.strip("/").split("/", 1)[0]
        query = parse_qs(parsed.query)
        start_param = (query.get("start") or query.get("startgroup") or [""])[0]
        if username:
            bot = await client.get_entity(username)
            peer = await message.get_input_chat()
            return await client(functions.messages.StartBotRequest(
                bot=bot,
                peer=peer,
                start_param=start_param,
            ))
            return
    # Для иных URL оставляем стандартное поведение Telethon.
    return await button.click()


class SessionCipher:
    def __init__(self, secret: str) -> None:
        try:
            self._fernet = Fernet(secret.encode())
        except (ValueError, TypeError) as exc:
            raise RuntimeError("SESSION_SECRET не является корректным ключом Fernet") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("Не удалось расшифровать сессию аккаунта") from exc


async def execute_farm(account: Account, config: Config, cipher: SessionCipher) -> str:
    session = StringSession(cipher.decrypt(account.session))
    client = TelegramClient(session, config.api_id, config.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сессия аккаунта завершена — добавьте аккаунт заново")

        target = await client.get_entity(config.target_chat)
        sent = await client.send_message(target, config.farm_command)

        deadline = asyncio.get_running_loop().time() + 35
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            messages = await client.get_messages(target, limit=15)
            for message in messages:
                if message.id <= sent.id or not message.buttons:
                    continue
                for row_index, row in enumerate(message.buttons):
                    for button_index, button in enumerate(row):
                        label = (button.text or "").strip().casefold()
                        if "собрать" in label:
                            await message.click(row_index, button_index)
                            return "✅ команда отправлена, кнопка «Собрать» нажата"
        raise RuntimeError("кнопка «Собрать» не появилась за 35 секунд")
    except FloodWaitError as exc:
        raise SafetyBlockError(
            "временный спам-блок",
            f"Telegram ограничил действия на {exc.seconds} сек.; аккаунт поставлен в карантин",
            exc.seconds,
        ) from exc
    except UserBannedInChannelError as exc:
        raise SafetyBlockError(
            "ограничение отправки в целевом чате, срок не указан",
            "Telegram запретил отправку в целевом чате; точный срок API не сообщает, аккаунт поставлен в карантин",
        ) from exc
    except (PeerFloodError, UserDeactivatedBanError) as exc:
        if isinstance(exc, UserDeactivatedBanError):
            raise SafetyBlockError(
                "постоянный бан аккаунта",
                "Telegram сообщил, что аккаунт деактивирован; аккаунт поставлен в карантин",
            ) from exc
        raise SafetyBlockError(
            "спам-блок, срок не указан",
            "Telegram вернул PeerFloodError; аккаунт поставлен в карантин",
        ) from exc
    finally:
        await client.disconnect()


async def execute_bonus(account: Account, config: Config, cipher: SessionCipher) -> tuple[str, int]:
    """Отправить «бонус», нажать кнопку и вернуть задержку до следующей проверки."""
    session = StringSession(cipher.decrypt(account.session))
    client = TelegramClient(session, config.api_id, config.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сессия аккаунта завершена — добавьте аккаунт заново")
        target = await client.get_entity(config.bonus_chat)
        sent = await client.send_message(target, config.bonus_command)
        deadline = asyncio.get_running_loop().time() + 60
        clicked = False
        timer_text = ""
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            messages = await client.get_messages(target, limit=20)
            for message in messages:
                # Бонусный бот может редактировать старое сообщение вместо
                # отправки нового. Учитываем edit_date такого сообщения.
                message_activity = message.edit_date or message.date
                sender = await message.get_sender()
                sender_username = (getattr(sender, "username", "") or "").casefold()
                message_text = message.raw_text or ""
                is_gram_by_text = "gram" in message_text.casefold() or "баланс" in message_text.casefold()
                is_gram = sender_username == config.bonus_bot_username or is_gram_by_text
                is_after_command = message.id >= sent.id or message_activity > sent.date
                if not is_gram or (not is_after_command and not message.buttons):
                    continue
                logging.getLogger(__name__).info(
                    "bonus response: id=%s sender=%s text=%r buttons=%s",
                    message.id,
                    sender_username or "<hidden>",
                    message_text[:300],
                    [button.text for row in (message.buttons or []) for button in row],
                )
                if is_after_command:
                    timer_text += f" {message_text}"
                if not clicked and message.buttons:
                    for row_index, row in enumerate(message.buttons):
                        for button_index, button in enumerate(row):
                            label = (button.text or "").strip().casefold()
                            if "бонус" in label:
                                callback_result = await _click_bonus_button(client, message, row_index, button_index)
                                clicked = True
                                callback_text = getattr(callback_result, "message", None)
                                if isinstance(callback_text, str):
                                    timer_text += f" {callback_text}"
                                break
                        if clicked:
                            break

            # Бот может вернуть только текст таймера без кнопки (например,
            # «Следующий бонус будет доступен через 1:16»). В этом случае
            # кнопку нажимать не нужно — следующий запуск назначается по тексту.
            wait = _timer_seconds(timer_text)
            if wait is not None:
                result = "✅ кнопка «Бонус» нажата" if clicked else "✅ таймер бонуса считан"
                return result, wait + config.bonus_extra_seconds
            if clicked:
                return "✅ кнопка «Бонус» нажата; таймер в ответе не передан", config.bonus_fallback_seconds
        raise RuntimeError(
            f"после команды «{config.bonus_command}» не найдены кнопка или сообщение с таймером за 35 секунд"
        )
    except FloodWaitError as exc:
        raise SafetyBlockError(
            "временный спам-блок",
            f"Telegram ограничил действия на {exc.seconds} сек.; аккаунт поставлен в карантин",
            exc.seconds,
        ) from exc
    except UserBannedInChannelError as exc:
        raise SafetyBlockError(
            "ограничение отправки в бонусном чате, срок не указан",
            "Telegram запретил отправку в бонусном чате; точный срок API не сообщает, аккаунт поставлен в карантин",
        ) from exc
    except (PeerFloodError, UserDeactivatedBanError) as exc:
        if isinstance(exc, UserDeactivatedBanError):
            raise SafetyBlockError(
                "постоянный бан аккаунта",
                "Telegram сообщил, что аккаунт деактивирован; аккаунт поставлен в карантин",
            ) from exc
        raise SafetyBlockError(
            "спам-блок, срок не указан",
            "Telegram вернул PeerFloodError; аккаунт поставлен в карантин",
        ) from exc
    finally:
        await client.disconnect()


async def execute_stall(account: Account, config: Config, cipher: SessionCipher) -> str:
    session = StringSession(cipher.decrypt(account.session))
    client = TelegramClient(session, config.api_id, config.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сессия аккаунта завершена — добавьте аккаунт заново")
        target = await client.get_entity(config.target_chat)
        sent = await client.send_message(target, config.stall_command)
        deadline = asyncio.get_running_loop().time() + 35
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            messages = await client.get_messages(target, limit=20)
            for message in messages:
                if message.id <= sent.id or not message.buttons:
                    continue
                for row_index, row in enumerate(message.buttons):
                    for button_index, button in enumerate(row):
                        if config.stall_button in (button.text or "").strip().casefold():
                            await message.click(row_index, button_index)
                            return "✅ ларёк: кнопка «Получить» нажата"
        raise RuntimeError("кнопка «Получить» не появилась за 35 секунд")
    except FloodWaitError as exc:
        raise SafetyBlockError("временный спам-блок", f"Telegram ограничил действия на {exc.seconds} сек.; аккаунт поставлен в карантин", exc.seconds) from exc
    except UserBannedInChannelError as exc:
        raise SafetyBlockError(
            "ограничение отправки в целевом чате, срок не указан",
            "Telegram запретил отправку в целевом чате; точный срок API не сообщает, аккаунт поставлен в карантин",
        ) from exc
    except (PeerFloodError, UserDeactivatedBanError) as exc:
        if isinstance(exc, UserDeactivatedBanError):
            raise SafetyBlockError("постоянный бан аккаунта", "Telegram сообщил, что аккаунт деактивирован; аккаунт поставлен в карантин") from exc
        raise SafetyBlockError("спам-блок, срок не указан", "Telegram вернул PeerFloodError; аккаунт поставлен в карантин") from exc
    finally:
        await client.disconnect()


ResultCallback = Callable[[Account, str], Awaitable[None]]


class FarmScheduler:
    def __init__(
        self,
        db: Database,
        config: Config,
        cipher: SessionCipher,
        on_result: ResultCallback | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.cipher = cipher
        self.on_result = on_result
        self._supervisor: asyncio.Task[None] | None = None
        self._running: set[int] = set()
        self._started_at = datetime.now(UTC)

    def start(self) -> None:
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._loop(), name="farm-scheduler")

    async def stop(self) -> None:
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = datetime.now(UTC)
            if now < self._started_at + timedelta(seconds=self.config.startup_delay_seconds):
                continue
            for account in await self.db.list_accounts(enabled_only=True):
                if account.id in self._running:
                    continue
                if account.available_from and datetime.fromisoformat(account.available_from) > now:
                    continue
                due = self.config.farm_enabled and (
                    account.next_run is None or datetime.fromisoformat(account.next_run) <= now
                )
                bonus_due = account.bonus_next_run is None or datetime.fromisoformat(account.bonus_next_run) <= now
                stall_due = account.stall_next_run is None or datetime.fromisoformat(account.stall_next_run) <= now
                if due:
                    asyncio.create_task(self.run_one(account.id), name=f"farm-account-{account.id}")
                    await asyncio.sleep(self.config.between_accounts_seconds)
                elif bonus_due:
                    asyncio.create_task(self.run_bonus(account.id), name=f"bonus-account-{account.id}")
                    await asyncio.sleep(self.config.between_accounts_seconds)
                elif stall_due:
                    asyncio.create_task(self.run_stall(account.id), name=f"stall-account-{account.id}")
                    await asyncio.sleep(self.config.between_accounts_seconds)

    async def run_one(self, account_id: int, *, force: bool = False) -> str:
        if account_id in self._running:
            return "⏳ Этот аккаунт уже выполняет задачу"
        account = await self.db.get_account(account_id)
        if not account:
            return "❌ Аккаунт не найден"

        now = datetime.now(UTC)
        if account.available_from:
            available_from = datetime.fromisoformat(account.available_from)
            if available_from > now:
                remaining = available_from - now
                minutes = max(1, int(remaining.total_seconds() // 60))
                hours, minutes = divmod(minutes, 60)
                return f"⏳ Новая сессия ожидает первого запуска ещё {hours} ч. {minutes} мин."
        if not force and account.next_run:
            next_run = datetime.fromisoformat(account.next_run)
            if next_run > now:
                remaining = next_run - now
                minutes = max(1, int(remaining.total_seconds() // 60))
                hours, minutes = divmod(minutes, 60)
                when = next_run.astimezone().strftime("%d.%m %H:%M")
                return (
                    f"⏱ Рано запускать: следующий запуск {when}. "
                    f"Осталось примерно {hours} ч. {minutes} мин."
                )

        self._running.add(account_id)
        try:
            status = await execute_farm(account, self.config, self.cipher)
        except SafetyBlockError as exc:
            status = _safety_status(exc, now)
            await self.db.quarantine(account_id, status)
        except Exception as exc:  # Ошибка сохраняется в статусе и не останавливает остальные аккаунты.
            status = f"❌ {type(exc).__name__}: {exc}"
        finally:
            self._running.discard(account_id)

        if not status.startswith("⛔"):
            next_run = now + timedelta(seconds=self.config.interval_seconds)
            await self.db.set_result(account_id, last_run=now, next_run=next_run, status=status)
        if self.on_result:
            await self.on_result(account, status)
        return status

    async def run_all(self) -> None:
        accounts = await self.db.list_accounts(enabled_only=True)
        for index, account in enumerate(accounts):
            await self.run_one(account.id)
            if index != len(accounts) - 1:
                await asyncio.sleep(self.config.between_accounts_seconds)

    async def run_bonus(self, account_id: int) -> str:
        if account_id in self._running:
            return "⏳ Этот аккаунт уже выполняет задачу"
        account = await self.db.get_account(account_id)
        if not account:
            return "❌ Аккаунт не найден"
        now = datetime.now(UTC)
        if account.available_from and datetime.fromisoformat(account.available_from) > now:
            return "⏳ Новая сессия ещё ожидает первого запуска"
        if account.bonus_next_run and datetime.fromisoformat(account.bonus_next_run) > now:
            return "⏱ Бонус ещё не доступен"
        self._running.add(account_id)
        try:
            status, delay = await execute_bonus(account, self.config, self.cipher)
            await self.db.set_bonus_result(account_id, next_run=now + timedelta(seconds=delay), status=status)
        except SafetyBlockError as exc:
            status = _safety_status(exc, now)
            await self.db.quarantine(account_id, status)
        except Exception as exc:
            status = f"❌ бонус: {type(exc).__name__}: {exc}"
            await self.db.set_bonus_result(account_id, next_run=now + timedelta(minutes=30), status=status)
        finally:
            self._running.discard(account_id)
        if self.on_result:
            await self.on_result(account, status)
        return status

    async def run_stall(self, account_id: int) -> str:
        if account_id in self._running:
            return "⏳ Этот аккаунт уже выполняет задачу"
        account = await self.db.get_account(account_id)
        if not account:
            return "❌ Аккаунт не найден"
        now = datetime.now(UTC)
        if account.available_from and datetime.fromisoformat(account.available_from) > now:
            return "⏳ Новая сессия ещё ожидает первого запуска"
        if account.stall_next_run and datetime.fromisoformat(account.stall_next_run) > now:
            return "⏱ Ларёк ещё не доступен"
        self._running.add(account_id)
        try:
            status = await execute_stall(account, self.config, self.cipher)
            await self.db.set_stall_result(account_id, next_run=now + timedelta(seconds=self.config.stall_interval_seconds), status=status)
        except SafetyBlockError as exc:
            status = _safety_status(exc, now)
            await self.db.quarantine(account_id, status)
        except Exception as exc:
            status = f"❌ ларёк: {type(exc).__name__}: {exc}"
            await self.db.set_stall_result(account_id, next_run=now + timedelta(minutes=30), status=status)
        finally:
            self._running.discard(account_id)
        if self.on_result:
            await self.on_result(account, status)
        return status
