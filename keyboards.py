from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import Account


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Аккаунты", callback_data="accounts")
    builder.button(text="📊 Статус", callback_data="status")
    builder.adjust(1)
    return builder.as_markup()


def accounts_menu(accounts: list[Account]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for account in accounts:
        icon = "🟢" if account.enabled else "⚪"
        builder.button(text=f"{icon} {account.display_name}", callback_data=f"account:{account.id}")
    builder.button(text="➕ Добавить аккаунт", callback_data="add_account")
    builder.button(text="⬅️ Главное меню", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def account_menu(account: Account) -> InlineKeyboardMarkup:
    enabled_text = "⏸ Отключить" if account.enabled else "▶️ Включить"
    builder = InlineKeyboardBuilder()
    builder.button(text="🌾 Запустить сейчас", callback_data=f"run:{account.id}")
    builder.button(text=enabled_text, callback_data=f"toggle:{account.id}")
    builder.button(text="🗑 Удалить", callback_data=f"delete_ask:{account.id}")
    builder.button(text="⬅️ К аккаунтам", callback_data="accounts")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить", callback_data=f"delete:{account_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"account:{account_id}")],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel_add")]]
    )


def code_keyboard(code_length: int = 5) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for digit in "123456789":
        builder.button(text=digit, callback_data=f"code:{digit}")
    builder.button(text="⌫", callback_data="code:back")
    builder.button(text="0", callback_data="code:0")
    builder.button(text="✅", callback_data="code:submit")
    builder.button(text="✖️ Отмена", callback_data="cancel_add")
    builder.adjust(3, 3, 3, 3, 1)
    return builder.as_markup()
