from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import Account, Chat


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Аккаунты", callback_data="accounts"),
             InlineKeyboardButton(text="💬 Пул чатов", callback_data="chats")],
            [InlineKeyboardButton(text="📊 Обзор системы", callback_data="status")],
            [InlineKeyboardButton(text="🚀 Накрутка", callback_data="boost")],
        ]
    )


def boost_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="😀 Реакции на пост", callback_data="boost:reactions")],
            [InlineKeyboardButton(text="👥 Подписчики", callback_data="boost:followers")],
            [InlineKeyboardButton(text="🔗 Реферальная ссылка", callback_data="boost:referral")],
            [InlineKeyboardButton(text="👁 Просмотры", callback_data="boost:views")],
            [InlineKeyboardButton(text="⌂ Главная", callback_data="home")],
        ]
    )


def reaction_groups_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Позитивные", callback_data="reaction_group:positive"),
                InlineKeyboardButton(text="Негативные", callback_data="reaction_group:negative"),
            ],
            [InlineKeyboardButton(text="Пропустить", callback_data="reaction_pick:auto")],
            [InlineKeyboardButton(text="Отмена", callback_data="boost")],
        ]
    )


def reactions_menu(group: str) -> InlineKeyboardMarkup:
    choices = (
        [("👍 Лайк", "👍"), ("🔥 Огонёк", "🔥"), ("❤️ Сердечко", "❤"),
         ("❤️‍🔥 Огненное сердце", "❤‍🔥"), ("💋 Губки", "💋")]
        if group == "positive"
        else [("👎 Дизлайк", "👎"), ("🤡 Клоун", "🤡")]
    )
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"reaction_pick:{emoji}")]
        for label, emoji in choices
    ]
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="reaction_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_started_menu(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отменить заказ", callback_data=f"order_cancel:{order_id}")],
            [InlineKeyboardButton(text="‹ Накрутка", callback_data="boost")],
        ]
    )


def accounts_menu(accounts: list[Account]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for account in accounts:
        icon = "★" if account.role == "admin_controller" else ("●" if account.enabled else "○")
        chat = "служебный аккаунт" if account.role == "admin_controller" else (account.bonus_chat_title or "без чата")
        builder.button(
            text=f"{icon} {account.display_name} · {chat}", callback_data=f"account:{account.id}"
        )
    builder.button(text="＋ Добавить аккаунт", callback_data="add_account")
    builder.button(text="⌂ Главная", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def add_account_method_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 По номеру телефона", callback_data="add_account_phone")],
            [InlineKeyboardButton(text="🗂 tdata ZIP (один или пачка)", callback_data="add_account_tdata")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_add")],
        ]
    )


def account_menu(account: Account) -> InlineKeyboardMarkup:
    enabled_text = "⏸ Остановить" if account.enabled else "▶ Включить"
    role_text = "Снять роль админа" if account.role == "admin_controller" else "★ Сделать админ-аккаунтом"
    role_action = "account_role_worker" if account.role == "admin_controller" else "account_role_admin"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶ Запустить ферму", callback_data=f"run:{account.id}"),
             InlineKeyboardButton(text="💰 Проверить бонус", callback_data=f"run_bonus:{account.id}")],
            [InlineKeyboardButton(text=role_text, callback_data=f"{role_action}:{account.id}")],
            [InlineKeyboardButton(text=enabled_text, callback_data=f"toggle:{account.id}"),
             InlineKeyboardButton(text="Удалить", callback_data=f"delete_ask:{account.id}")],
            [InlineKeyboardButton(text="‹ Аккаунты", callback_data="accounts")],
        ]
    )


def chats_menu(chats: list[Chat], limit: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        icon = "●" if chat.enabled else "○"
        builder.button(
            text=f"{icon} {chat.title} · {chat.account_count}/{limit}",
            callback_data=f"chat:{chat.id}",
        )
    builder.button(text="＋ Добавить чат", callback_data="add_chat")
    builder.button(text="⌂ Главная", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def chat_menu(chat: Chat) -> InlineKeyboardMarkup:
    enabled_text = "⏸ Не назначать новые" if chat.enabled else "▶ Вернуть в распределение"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=enabled_text, callback_data=f"chat_toggle:{chat.id}")],
            [InlineKeyboardButton(text="Удалить из пула", callback_data=f"chat_delete_ask:{chat.id}")],
            [InlineKeyboardButton(text="‹ Пул чатов", callback_data="chats")],
        ]
    )


def confirm_delete(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить аккаунт", callback_data=f"delete:{account_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"account:{account_id}")],
        ]
    )


def confirm_chat_delete(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить и распределить", callback_data=f"chat_delete:{chat_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"chat:{chat_id}")],
        ]
    )


def cancel_menu(callback_data: str = "cancel_add") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=callback_data)]]
    )


def code_keyboard(code_length: int = 5) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for digit in "123456789":
        builder.button(text=digit, callback_data=f"code:{digit}")
    builder.button(text="⌫", callback_data="code:back")
    builder.button(text="0", callback_data="code:0")
    builder.button(text="Готово", callback_data="code:submit")
    builder.button(text="Отмена", callback_data="cancel_add")
    builder.adjust(3, 3, 3, 3, 1)
    return builder.as_markup()
