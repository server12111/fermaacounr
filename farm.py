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
from telethon.tl.types import (
    ChannelParticipantsRecent,
    ChatAdminRights,
    InputUser,
    ReactionEmoji,
    User,
)
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
)
from telethon.sessions import StringSession

from config import Config
from db import Account, Database, Order


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


def _extract_gram_balance(text: str) -> int | None:
    """Parse an integer GRAM balance without turning decimal fractions into extra digits."""
    match = re.search(
        r"баланс\s*:?\s*([0-9][0-9\s\u00a0.,]*)\s*(?:gram|грам)",
        text.casefold(),
    )
    if not match:
        return None
    raw = match.group(1).strip().replace("\u00a0", " ")
    # Spaces are thousands separators. A final comma/dot followed by 1-2 digits
    # is a decimal fraction and must not inflate 7500.50 into 750050.
    normalized = re.sub(r"\s+", "", raw)
    decimal = re.search(r"[.,](\d{1,2})$", normalized)
    if decimal:
        normalized = normalized[:decimal.start()]
    digits = re.sub(r"\D", "", normalized)
    return int(digits) if digits else None



async def _ensure_joined(client: TelegramClient, reference: str):
    """Вернуть чат и при необходимости вступить в него текущим аккаунтом."""
    value = reference.strip()
    parsed = urlparse(value if "://" in value else f"https://t.me/{value.lstrip('@')}")
    path = parsed.path.strip("/")
    invite_hash = ""
    if parsed.netloc.casefold() in {"t.me", "telegram.me"}:
        if path.startswith("+"):
            invite_hash = path[1:]
        elif path.startswith("joinchat/"):
            invite_hash = path.split("/", 1)[1]

    if invite_hash:
        try:
            result = await client(functions.messages.ImportChatInviteRequest(invite_hash))
            if result.chats:
                return result.chats[0]
        except UserAlreadyParticipantError:
            check = await client(functions.messages.CheckChatInviteRequest(invite_hash))
            chat = getattr(check, "chat", None)
            if chat:
                return chat
        raise RuntimeError("не удалось открыть чат по пригласительной ссылке")

    target_ref = path.split("/", 1)[0] if parsed.netloc else value
    target = await client.get_entity(target_ref or value)
    try:
        await client(functions.channels.JoinChannelRequest(target))
    except UserAlreadyParticipantError:
        pass
    return target


def _telegram_parts(reference: str) -> tuple[list[str], dict[str, list[str]]]:
    value = reference.strip()
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("нужна полная ссылка https://t.me/...")
    if (parsed.hostname or "").casefold() not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        raise ValueError("разрешены только ссылки t.me или telegram.me")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("пустая Telegram-ссылка")
    return parts, parse_qs(parsed.query)


async def _post_target(client: TelegramClient, reference: str):
    parts, _ = _telegram_parts(reference)
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] == "c":
        if not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError("неверная ссылка на пост")
        try:
            peer = await client.get_entity(int(f"-100{parts[1]}"))
        except ValueError:
            await client.get_dialogs(limit=None)
            peer = await client.get_entity(int(f"-100{parts[1]}"))
        return peer, int(parts[-1])
    if len(parts) < 2 or not parts[-1].isdigit():
        raise ValueError("нужна полная ссылка на пост")
    peer = await client.get_entity(parts[0])
    return peer, int(parts[-1])


async def _start_bot_link(client: TelegramClient, reference: str) -> None:
    parts, query = _telegram_parts(reference)
    if not parts:
        raise ValueError("в ссылке не указан бот")
    bot = await client.get_entity(parts[0])
    if not getattr(bot, "bot", False):
        raise ValueError("реферальная ссылка должна вести на бота")
    parameter = (query.get("start") or query.get("startgroup") or [""])[0]
    await client(functions.messages.StartBotRequest(bot=bot, peer=bot, start_param=parameter))


async def _visit_link(client: TelegramClient, reference: str) -> str:
    parts, query = _telegram_parts(reference)
    if parts[0].startswith("+") or parts[0] == "joinchat":
        try:
            await _ensure_joined(client, reference)
            return "вступил"
        except InviteRequestSentError:
            return "заявка отправлена"
    entity = await client.get_entity(parts[0])
    if getattr(entity, "bot", False):
        parameter = (query.get("start") or query.get("startgroup") or [""])[0]
        await client(functions.messages.StartBotRequest(bot=entity, peer=entity, start_param=parameter))
        return "бот запущен"
    if isinstance(entity, User):
        # Telegram has no follower relationship for ordinary users. The closest supported
        # operation is adding the public profile to contacts, and the result says so.
        await client(functions.contacts.AddContactRequest(
            id=entity,
            first_name=entity.first_name or entity.username or "Telegram",
            last_name=entity.last_name or "",
            phone="",
            add_phone_privacy_exception=False,
        ))
        return "пользователь добавлен в контакты"
    try:
        await _ensure_joined(client, reference)
        return "вступил"
    except InviteRequestSentError:
        return "заявка отправлена"


async def execute_engagement_action(
    account: Account,
    config: Config,
    cipher: "SessionCipher",
    *,
    kind: str,
    link: str,
    reaction: str = "auto",
    prerequisite_links: list[str] | None = None,
    cooldown_seconds: int = 0,
) -> str:
    client = TelegramClient(
        StringSession(cipher.decrypt(account.session)), config.api_id, config.api_hash
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
            raise RuntimeError("сессия аккаунта завершена")
        if kind == "reactions":
            peer, message_id = await _post_target(client, link)
            if reaction == "auto":
                message = await client.get_messages(peer, ids=message_id)
                results = getattr(getattr(message, "reactions", None), "results", None) or []
                selected = max(results, key=lambda item: item.count).reaction if results else None
                reaction_value = selected or ReactionEmoji(emoticon="👍")
            else:
                reaction_value = ReactionEmoji(emoticon=reaction)
            await asyncio.wait_for(client(functions.messages.SendReactionRequest(
                peer=peer, msg_id=message_id, reaction=[reaction_value],
                big=False, add_to_recent=False,
            )), timeout=30)
            return f"реакция {getattr(reaction_value, 'emoticon', 'custom')} поставлена"
        elif kind == "followers":
            return await asyncio.wait_for(_visit_link(client, link), timeout=45)
        elif kind == "referral":
            details = []
            for prerequisite in prerequisite_links or []:
                details.append(await asyncio.wait_for(_visit_link(client, prerequisite), timeout=45))
            await asyncio.wait_for(_start_bot_link(client, link), timeout=30)
            return "реферальный бот запущен" + (f"; подготовка: {', '.join(details)}" if details else "")
        elif kind == "views":
            peer, message_id = await _post_target(client, link)
            result = await asyncio.wait_for(client(functions.messages.GetMessagesViewsRequest(
                peer=peer, id=[message_id], increment=True
            )), timeout=30)
            if not getattr(result, "views", None):
                raise RuntimeError("Telegram не вернул счётчик просмотра")
            return "просмотр запрошен и подтверждён Telegram"
        else:
            raise ValueError("неизвестный тип заказа")
    except FloodWaitError as exc:
        raise SafetyBlockError(
            "временный спам-блок", f"Telegram ограничил действия на {exc.seconds} сек.", exc.seconds
        ) from exc
    except (PeerFloodError, UserDeactivatedBanError) as exc:
        raise SafetyBlockError("спам-блок", str(exc)) from exc
    finally:
        await client.disconnect()


async def _is_expected_bot_response(message, sent, expected_username: str = "") -> bool:
    if message.id <= sent.id or not message.buttons:
        return False
    reply_to = getattr(message, "reply_to_msg_id", None)
    if reply_to is not None and reply_to != sent.id:
        return False
    sender = await message.get_sender()
    if not getattr(sender, "bot", False):
        return False
    username = (getattr(sender, "username", "") or "").casefold()
    return not expected_username or username == expected_username


async def grant_full_admin(service: Account, worker: Account, chat_reference: str, config: Config, cipher: SessionCipher) -> str:
    """Служебный аккаунт выдаёт рабочему права на отправку и удаление сообщений в чате."""
    service_client = TelegramClient(StringSession(cipher.decrypt(service.session)), config.api_id, config.api_hash)
    worker_client = TelegramClient(StringSession(cipher.decrypt(worker.session)), config.api_id, config.api_hash)
    try:
        await service_client.connect()
        await worker_client.connect()
        if not await service_client.is_user_authorized() or not await worker_client.is_user_authorized():
            raise RuntimeError("сессия служебного или рабочего аккаунта завершена")
        service_chat = await _ensure_joined(service_client, chat_reference)
        await _ensure_joined(worker_client, chat_reference)
        worker_me = await worker_client.get_me()
        # GetParticipantRequest требует заранее знать access_hash воркера, а
        # служебный аккаунт может ни разу не «видеть» его вне этого канала —
        # точечный запрос падает либо на резолве participant (get_input_entity),
        # либо с ParticipantIdInvalidError при hash=0. Вместо этого читаем
        # список последних участников: сервер отдаёт валидный access_hash без
        # необходимости знать его заранее, а воркер только что вступил и
        # окажется среди последних.
        service_view = None
        offset = 0
        while service_view is None and offset < 400:
            result = await service_client(functions.channels.GetParticipantsRequest(
                channel=service_chat, filter=ChannelParticipantsRecent(),
                offset=offset, limit=100, hash=0,
            ))
            if not result.users:
                break
            service_view = next((user for user in result.users if user.id == worker_me.id), None)
            offset += len(result.users)
            if len(result.users) < 100:
                break
        if service_view is None:
            raise RuntimeError("служебный аккаунт не видит рабочего участника")
        if service_view.access_hash is None:
            raise RuntimeError("нет access_hash рабочего участника")
        worker_entity = InputUser(service_view.id, service_view.access_hash)
        rights = ChatAdminRights(
            post_messages=True, delete_messages=True,
        )
        await service_client(functions.channels.EditAdminRequest(
            channel=service_chat, user_id=worker_entity, admin_rights=rights, rank="Farm worker"
        ))
        return "✅ права на отправку и удаление сообщений выданы"
    finally:
        await worker_client.disconnect()
        await service_client.disconnect()


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
            return await client(functions.messages.StartBotRequest(
                bot=bot,
                peer=bot,
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
                if not await _is_expected_bot_response(message, sent, config.target_bot_username):
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


async def execute_bonus(account: Account, config: Config, cipher: SessionCipher, db: Database) -> tuple[str, int]:
    """Отправить «бонус», нажать кнопку и вернуть задержку до следующей проверки."""
    session = StringSession(cipher.decrypt(account.session))
    client = TelegramClient(session, config.api_id, config.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сессия аккаунта завершена — добавьте аккаунт заново")
        chat_reference = account.bonus_chat_reference or config.bonus_chat
        target = await _ensure_joined(client, chat_reference)
        sent = await client.send_message(target, config.bonus_command)
        deadline = asyncio.get_running_loop().time() + 60
        clicked = False
        clicked_at: float | None = None
        timer_text = ""
        processed_balance_ids: set[tuple[int, str]] = set()
        payout_note = ""
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
                is_gram = sender_username == config.bonus_bot_username
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
                    revision = (
                        message.id,
                        message_activity.isoformat() if message_activity else message_text,
                    )
                    if revision not in processed_balance_ids:
                        processed_balance_ids.add(revision)
                        balance = _extract_gram_balance(message_text)
                        # Some bots keep editing one persistent message. A revision-based
                        # source id lets a later edit trigger a new payout while remaining
                        # idempotent for repeated polling of the same edit.
                        payout_source_id = message.id
                        if message.edit_date:
                            payout_source_id = -(
                                (message.id << 31)
                                | (int(message.edit_date.timestamp()) & 0x7FFFFFFF)
                            )
                        # Бонусный бот не всегда отвечает через reply или
                        # настоящее упоминание (entity) — часто это просто общий
                        # чат, а первой строкой открытым текстом написано имя
                        # аккаунта ("Avalon\n💰 Баланс: ..."), без reply_to и без
                        # mention-сущности. Поэтому дополнительно сверяем первую
                        # строку ответа с display_name аккаунта.
                        first_line = message_text.strip().splitlines()[0].strip().casefold() if message_text.strip() else ""
                        name_matches = bool(account.display_name) and first_line == account.display_name.strip().casefold()
                        owns_balance = bool(
                            getattr(message, "reply_to_msg_id", None) == sent.id
                            or getattr(message, "mentioned", False)
                            or name_matches
                        )
                        if balance is not None:
                            if not owns_balance:
                                payout_note = "выплата пропущена: ответ не привязан к аккаунту"
                            elif not config.payout_enabled:
                                payout_note = "выплата выключена: установите PAYOUT_ENABLED=true"
                            elif balance < config.payout_threshold:
                                payout_note = f"баланс {balance}, порог {config.payout_threshold}"
                            elif balance > config.payout_max_amount:
                                payout_note = f"баланс {balance} выше защитного лимита {config.payout_max_amount}"
                            else:
                                logging.getLogger(__name__).info(
                                    "GRAM balance for %s: %s", account.display_name, balance
                                )
                                claimed = await db.claim_payout(
                                    account.id, int(target.id), payout_source_id, balance
                                )
                                if claimed:
                                    payout_text = (
                                        f"{config.payout_command} "
                                        f"{config.payout_recipient_id} {balance}"
                                    )
                                    try:
                                        await client.send_message(target, payout_text)
                                    except Exception:
                                        await db.release_payout(
                                            account.id, int(target.id), payout_source_id
                                        )
                                        raise
                                    await db.complete_payout(
                                        account.id, int(target.id), payout_source_id
                                    )
                                    payout_note = f"выплата {balance}: команда отправлена"
                                    logging.getLogger(__name__).info(
                                        "Payout command sent for account %s, source message %s",
                                        account.id, message.id,
                                    )
                                else:
                                    payout_note = "выплата уже обработана"
                if not clicked and message.buttons:
                    for row_index, row in enumerate(message.buttons):
                        for button_index, button in enumerate(row):
                            label = (button.text or "").strip().casefold()
                            if "бонус" in label:
                                await _click_bonus_button(client, message, row_index, button_index)
                                clicked = True
                                clicked_at = asyncio.get_running_loop().time()
                                break
                        if clicked:
                            break
            if clicked_at is not None and asyncio.get_running_loop().time() - clicked_at >= 15:
                parsed_delay = _timer_seconds(timer_text)
                delay = (parsed_delay + config.bonus_extra_seconds) if parsed_delay else config.bonus_interval_seconds
                status = "✅ кнопка «Бонус» нажата, ответ проверен"
                if payout_note:
                    status += f"; {payout_note}"
                return status, delay

            parsed_delay = _timer_seconds(timer_text)
            if not clicked and parsed_delay is not None:
                status = "⏱ бонус ещё не доступен; запуск назначен по таймеру"
                if payout_note:
                    status += f"; {payout_note}"
                return status, parsed_delay + config.bonus_extra_seconds
        status = "⚠️ кнопка и таймер «Бонус» не найдены; следующая проверка позже"
        if payout_note:
            status += f"; {payout_note}"
        return status, config.bonus_fallback_seconds
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
                if not await _is_expected_bot_response(message, sent, config.target_bot_username):
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
OrderCallback = Callable[[Order, int, int], Awaitable[None]]


class FarmScheduler:
    def __init__(
        self, db: Database, config: Config, cipher: SessionCipher,
        on_result: ResultCallback | None = None,
        on_order_result: OrderCallback | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.cipher = cipher
        self.on_result = on_result
        self.on_order_result = on_order_result
        self._supervisor: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._order_tasks: dict[int, asyncio.Task] = {}
        self._running: set[int] = set()
        self._reservation_lock = asyncio.Lock()
        self._service_lock = asyncio.Lock()
        self._stopping = False
        self._started_at = datetime.now(UTC)

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        if self._stopping:
            raise RuntimeError("планировщик останавливается")
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start(self) -> None:
        if self._supervisor is None:
            self._stopping = False
            self._supervisor = asyncio.create_task(self._loop(), name="farm-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor:
            self._supervisor.cancel()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*(tasks + ([self._supervisor] if self._supervisor else [])), return_exceptions=True)
        self._tasks.clear()
        self._order_tasks.clear()
        self._supervisor = None

    async def _reserve(self, account_ids: list[int]) -> list[int]:
        async with self._reservation_lock:
            reserved = [account_id for account_id in account_ids if account_id not in self._running]
            self._running.update(reserved)
            return reserved

    async def _release(self, account_ids: set[int] | list[int]) -> None:
        async with self._reservation_lock:
            self._running.difference_update(account_ids)

    async def available_order_count(self) -> int:
        now = datetime.now(UTC)
        accounts = await self.db.list_accounts(enabled_only=True)
        async with self._reservation_lock:
            return sum(
                account.role == "worker" and bool(account.session)
                and account.id not in self._running
                and (not account.available_from or datetime.fromisoformat(account.available_from) <= now)
                for account in accounts
            )

    def launch_order(self, order_id: int) -> None:
        if order_id in self._order_tasks or self._stopping:
            return
        task = self._spawn(self.run_engagement_order(order_id), name=f"engagement-order-{order_id}")
        self._order_tasks[order_id] = task
        task.add_done_callback(lambda _task, oid=order_id: self._order_tasks.pop(oid, None))

    def launch_manual(self, account_id: int) -> None:
        self._spawn(self.run_one(account_id, force=True), name=f"manual-account-{account_id}")

    def launch_bonus_manual(self, account_id: int) -> None:
        self._spawn(
            self.run_bonus(account_id, force=True),
            name=f"manual-bonus-account-{account_id}",
        )

    async def is_account_running(self, account_id: int) -> bool:
        async with self._reservation_lock:
            return account_id in self._running

    async def _order_delay(self, order_id: int, seconds: int) -> bool:
        remaining = seconds
        while remaining > 0:
            step = min(5, remaining)
            await asyncio.sleep(step)
            remaining -= step
            order = await self.db.get_order(order_id)
            if not order or order.status == "cancelling":
                return False
        return True

    async def run_engagement_order(self, order_id: int) -> tuple[int, int]:
        order = await self.db.get_order(order_id)
        if not order:
            return 0, 1
        if order.status == "cancelling":
            result = await self.db.finish_order(order_id, "cancelled")
            if self.on_order_result:
                await self.on_order_result(order, *result)
            return result

        pending = await self.db.list_order_items(order_id)
        if not pending and order.started_at is not None:
            completed, errors = await self.db.finish_order(order_id, "completed")
            finished = await self.db.get_order(order_id) or order
            if self.on_order_result:
                await self.on_order_result(finished, completed, errors)
            return completed, errors
        if not pending:
            now = datetime.now(UTC)
            accounts = [
                account for account in await self.db.list_accounts(enabled_only=True)
                if account.role == "worker" and account.session
                and (not account.available_from or datetime.fromisoformat(account.available_from) <= now)
            ]
            # Резервируем только на момент отбора кандидатов (чтобы два заказа не
            # выбрали один аккаунт одновременно), а сразу после сохранения списка
            # в БД отпускаем — иначе аккаунт был бы заблокирован для фермы/бонуса
            # на всё время заказа, включая cooldown между аккаунтами.
            candidates = await self._reserve([account.id for account in accounts])
            selected = candidates[:order.requested_quantity]
            await self._release(set(candidates))
            await self.db.prepare_order(order_id, selected, max(0, order.requested_quantity - len(selected)))
            pending = selected

        for index, account_id in enumerate(pending):
            current = await self.db.get_order(order_id)
            if not current or current.status == "cancelling":
                await self.db.set_order_item(order_id, account_id, "cancelled", "Отменено оператором")
                for remaining in pending[index + 1:]:
                    await self.db.set_order_item(order_id, remaining, "cancelled", "Отменено оператором")
                break
            # Резервируем аккаунт только на время его собственного RPC, а не на
            # весь заказ — до и после (в том числе во время cooldown) он остаётся
            # доступен для плановых циклов фермы/бонуса.
            if not await self._reserve([account_id]):
                await self.db.set_order_item(order_id, account_id, "error", "Аккаунт занят другой операцией")
                continue
            try:
                account = await self.db.get_account(account_id)
                if not account or not account.enabled:
                    await self.db.set_order_item(order_id, account_id, "error", "Аккаунт отсутствует или выключен")
                    continue
                await self.db.set_order_item(order_id, account_id, "running", "RPC выполняется")
                try:
                    detail = await execute_engagement_action(
                        account, self.config, self.cipher, kind=order.kind, link=order.link,
                        reaction=order.reaction, prerequisite_links=order.prerequisite_links,
                        cooldown_seconds=order.cooldown_seconds,
                    )
                    await self.db.set_order_item(order_id, account_id, "success", detail)
                except SafetyBlockError as exc:
                    status = _safety_status(exc, datetime.now(UTC))
                    await self.db.quarantine(account.id, status)
                    await self.db.set_order_item(order_id, account_id, "error", status)
                except asyncio.CancelledError:
                    await self.db.set_order_item(order_id, account_id, "error", "Прервано остановкой")
                    raise
                except Exception as exc:
                    logging.getLogger(__name__).exception("Ошибка заказа %s на аккаунте %s", order_id, account_id)
                    await self.db.set_order_item(order_id, account_id, "error", f"{type(exc).__name__}: {exc}")
            finally:
                await self._release([account_id])
            if order.kind in {"followers", "referral"} and order.cooldown_seconds and index != len(pending) - 1:
                if not await self._order_delay(order_id, order.cooldown_seconds):
                    for remaining in pending[index + 1:]:
                        await self.db.set_order_item(order_id, remaining, "cancelled", "Отменено оператором")
                    break

        current = await self.db.get_order(order_id)
        final_status = "cancelled" if current and current.status == "cancelling" else "completed"
        completed, errors = await self.db.finish_order(order_id, final_status)
        finished = await self.db.get_order(order_id) or order
        if self.on_order_result:
            await self.on_order_result(finished, completed, errors)
        return completed, errors

    async def _loop(self) -> None:
        while True:
            try:
                for order in await self.db.list_recoverable_orders():
                    self.launch_order(order.id)
                await asyncio.sleep(10)
                now = datetime.now(UTC)
                if now < self._started_at + timedelta(seconds=self.config.startup_delay_seconds):
                    continue
                for account in await self.db.list_accounts(enabled_only=True):
                    try:
                        if account.role != "worker" or account.id in self._running:
                            continue
                        if account.available_from and datetime.fromisoformat(account.available_from) > now:
                            continue
                        due = self.config.farm_enabled and (not account.next_run or datetime.fromisoformat(account.next_run) <= now)
                        bonus_due = not account.bonus_next_run or datetime.fromisoformat(account.bonus_next_run) <= now
                        stall_due = self.config.stall_enabled and (not account.stall_next_run or datetime.fromisoformat(account.stall_next_run) <= now)
                        if due:
                            self._spawn(self.run_one(account.id), name=f"farm-account-{account.id}")
                        elif bonus_due:
                            self._spawn(self.run_bonus(account.id), name=f"bonus-account-{account.id}")
                        elif stall_due:
                            self._spawn(self.run_stall(account.id), name=f"stall-account-{account.id}")
                        else:
                            continue
                    except Exception:
                        # Один повреждённый/некорректный аккаунт не должен
                        # прерывать проход по всем остальным — иначе цикл падает
                        # в общий except, начинается заново с первого аккаунта,
                        # и все аккаунты после проблемного (обычно новые, с
                        # большими id) никогда не доходят до обработки.
                        logging.getLogger(__name__).exception(
                            "Ошибка планирования аккаунта %s; пропуск", account.id
                        )
                        continue
                    await asyncio.sleep(self.config.between_accounts_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Ошибка supervisor; повтор через 10 секунд")
                await asyncio.sleep(10)

    async def _grant_worker_admin(self, account: Account, chat_reference: str) -> str | None:
        if account.role != "worker":
            return None
        service = await self.db.get_admin_controller()
        if not service:
            return None
        try:
            async with self._service_lock:
                return await asyncio.wait_for(
                    grant_full_admin(service, account, chat_reference, self.config, self.cipher), timeout=60
                )
        except Exception as exc:
            return f"⚠️ админ-права не выданы: {type(exc).__name__}: {exc}"

    async def run_one(self, account_id: int, *, force: bool = False) -> str:
        if not await self._reserve([account_id]):
            return "⏳ Этот аккаунт уже выполняет задачу"
        try:
            account = await self.db.get_account(account_id)
            if not account or not account.enabled:
                return "❌ Аккаунт не найден или выключен"
            if account.role != "worker":
                return "⛔ Служебный аккаунт не выполняет рабочие команды"
            now = datetime.now(UTC)
            if account.available_from and datetime.fromisoformat(account.available_from) > now:
                remaining = datetime.fromisoformat(account.available_from) - now
                minutes = max(1, int(remaining.total_seconds() // 60))
                hours, minutes = divmod(minutes, 60)
                return f"⏳ Новая сессия ожидает первого запуска ещё {hours} ч. {minutes} мин."
            if not force and account.next_run and datetime.fromisoformat(account.next_run) > now:
                next_run = datetime.fromisoformat(account.next_run)
                return f"⏱ Рано запускать: следующий запуск {next_run.astimezone():%d.%m %H:%M}."
            if not self.config.farm_enabled:
                return "⛔ Ферма отключена (FARM_ENABLED=false) — запуск пропущен"
            # admin_status может провалиться до того, как execute_farm бросит
            # исключение — если прицепить предупреждение только в happy-path,
            # реальная причина (не выданы права) теряется, и виден только
            # производный сбой отправки.
            admin_status = None
            try:
                admin_status = await self._grant_worker_admin(account, self.config.target_chat)
                status = await asyncio.wait_for(execute_farm(account, self.config, self.cipher), timeout=90)
            except SafetyBlockError as exc:
                status = _safety_status(exc, now)
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.quarantine(account_id, status)
            except Exception as exc:
                status = f"❌ {type(exc).__name__}: {exc}"
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
            else:
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
            if not status.startswith("⛔"):
                await self.db.set_result(account_id, last_run=now,
                    next_run=now + timedelta(seconds=self.config.interval_seconds), status=status)
            if self.on_result:
                await self.on_result(account, status)
            return status
        finally:
            await self._release([account_id])

    async def run_all(self) -> None:
        accounts = await self.db.list_accounts(enabled_only=True)
        for index, account in enumerate(accounts):
            await self.run_one(account.id)
            if index != len(accounts) - 1:
                await asyncio.sleep(self.config.between_accounts_seconds)

    async def run_bonus(self, account_id: int, *, force: bool = False) -> str:
        if not await self._reserve([account_id]):
            return "⏳ Этот аккаунт уже выполняет задачу"
        try:
            account = await self.db.get_account(account_id)
            if not account or not account.enabled:
                return "❌ Аккаунт не найден или выключен"
            if account.role != "worker":
                return "⛔ Служебный аккаунт не выполняет рабочие команды"
            now = datetime.now(UTC)
            if account.available_from and datetime.fromisoformat(account.available_from) > now:
                return "⏳ Новая сессия ещё ожидает первого запуска"
            if not force and account.bonus_next_run and datetime.fromisoformat(account.bonus_next_run) > now:
                return "⏱ Бонус ещё не доступен"
            admin_status = None
            try:
                if not account.bonus_chat_reference:
                    status, delay = "❌ бонус: аккаунту не назначен чат", self.config.bonus_retry_seconds
                else:
                    admin_status = await self._grant_worker_admin(account, account.bonus_chat_reference)
                    status, delay = await asyncio.wait_for(
                        execute_bonus(account, self.config, self.cipher, self.db), timeout=100
                    )
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.set_bonus_result(account_id, next_run=now + timedelta(seconds=delay), status=status)
            except SafetyBlockError as exc:
                status = _safety_status(exc, now)
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.quarantine(account_id, status)
            except Exception as exc:
                status = f"❌ бонус: {type(exc).__name__}: {exc}"
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.set_bonus_result(account_id, next_run=now + timedelta(minutes=30), status=status)
            if self.on_result:
                await self.on_result(account, status)
            return status
        finally:
            await self._release([account_id])

    async def run_stall(self, account_id: int) -> str:
        if not await self._reserve([account_id]):
            return "⏳ Этот аккаунт уже выполняет задачу"
        try:
            account = await self.db.get_account(account_id)
            if not account or not account.enabled:
                return "❌ Аккаунт не найден или выключен"
            if account.role != "worker":
                return "⛔ Служебный аккаунт не выполняет рабочие команды"
            now = datetime.now(UTC)
            if account.available_from and datetime.fromisoformat(account.available_from) > now:
                return "⏳ Новая сессия ещё ожидает первого запуска"
            if account.stall_next_run and datetime.fromisoformat(account.stall_next_run) > now:
                return "⏱ Ларёк ещё не доступен"
            admin_status = None
            try:
                admin_status = await self._grant_worker_admin(account, self.config.target_chat)
                status = await asyncio.wait_for(execute_stall(account, self.config, self.cipher), timeout=90)
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.set_stall_result(account_id,
                    next_run=now + timedelta(seconds=self.config.stall_interval_seconds), status=status)
            except SafetyBlockError as exc:
                status = _safety_status(exc, now)
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.quarantine(account_id, status)
            except Exception as exc:
                status = f"❌ ларёк: {type(exc).__name__}: {exc}"
                if admin_status and admin_status.startswith("⚠️"):
                    status = f"{admin_status}; {status}"
                await self.db.set_stall_result(account_id, next_run=now + timedelta(minutes=30), status=status)
            if self.on_result:
                await self.on_result(account, status)
            return status
        finally:
            await self._release([account_id])

