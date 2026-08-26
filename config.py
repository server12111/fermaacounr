from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    api_id: int
    api_hash: str
    session_secret: str
    target_chat: str
    target_bot_username: str
    farm_command: str
    farm_enabled: bool
    stall_enabled: bool
    bonus_chat: str
    chat_account_limit: int
    bonus_command: str
    bonus_bot_username: str
    bonus_extra_seconds: int
    bonus_fallback_seconds: int
    bonus_interval_seconds: int
    bonus_retry_seconds: int
    payout_enabled: bool
    payout_threshold: int
    payout_max_amount: int
    payout_recipient_id: int
    payout_command: str
    stall_command: str
    stall_button: str
    stall_interval_seconds: int
    interval_seconds: int
    startup_delay_seconds: int
    between_accounts_seconds: int
    database_path: str = "data/farm.db"


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом") from exc


def _env_hours(name: str, default: str) -> int:
    try:
        return int(float(os.getenv(name, default)) * 3600)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть числом часов") from exc


def load_config() -> Config:
    load_dotenv()
    required = ["BOT_TOKEN", "ADMIN_IDS", "TG_API_ID", "TG_API_HASH", "SESSION_SECRET"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

    try:
        admin_ids = frozenset(
            int(value.strip()) for value in os.environ["ADMIN_IDS"].split(",") if value.strip()
        )
        api_id = int(os.environ["TG_API_ID"])
    except ValueError as exc:
        raise RuntimeError("ADMIN_IDS и TG_API_ID должны содержать числа") from exc
    if not admin_ids:
        raise RuntimeError("В ADMIN_IDS должен быть хотя бы один Telegram ID")

    data_dir = os.getenv("DATA_DIR", "data")
    # /app/data используется на Linux-сервере; локальный Windows-запуск
    # сохраняет базу в каталоге проекта.
    if os.name == "nt" and data_dir.startswith("/"):
        data_dir = "data"

    config = Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=admin_ids,
        api_id=api_id,
        api_hash=os.environ["TG_API_HASH"],
        session_secret=os.environ["SESSION_SECRET"],
        target_chat=os.getenv("TARGET_CHAT", "@VirusikChat"),
        target_bot_username=os.getenv("TARGET_BOT_USERNAME", "").lstrip("@").casefold(),
        farm_command=os.getenv("FARM_COMMAND", "ферма"),
        farm_enabled=os.getenv("FARM_ENABLED", "false").casefold() in {"1", "true", "yes", "on"},
        stall_enabled=os.getenv("STALL_ENABLED", "false").casefold() in {"1", "true", "yes", "on"},
        bonus_chat=os.getenv("BONUS_CHAT", "https://t.me/piar_grames"),
        chat_account_limit=_env_int("CHAT_ACCOUNT_LIMIT", "45"),
        bonus_command=os.getenv("BONUS_COMMAND", "б"),
        bonus_bot_username=os.getenv("BONUS_BOT_USERNAME", "valyutaTG_bot").lstrip("@").casefold(),
        bonus_extra_seconds=_env_int("BONUS_EXTRA_MINUTES", "5") * 60,
        bonus_fallback_seconds=_env_int("BONUS_FALLBACK_MINUTES", "30") * 60,
        bonus_interval_seconds=_env_hours("BONUS_INTERVAL_HOURS", "12"),
        bonus_retry_seconds=_env_int("BONUS_RETRY_MINUTES", "30") * 60,
        payout_enabled=os.getenv("PAYOUT_ENABLED", "false").casefold() in {"1", "true", "yes", "on"},
        payout_threshold=_env_int("PAYOUT_THRESHOLD", "7500"),
        payout_max_amount=_env_int("PAYOUT_MAX_AMOUNT", "50000"),
        payout_recipient_id=_env_int("PAYOUT_RECIPIENT_ID", "7145919720"),
        payout_command=os.getenv("PAYOUT_COMMAND", "П"),
        stall_command=os.getenv("STALL_COMMAND", "ларек"),
        stall_button=os.getenv("STALL_BUTTON", "собрать"),
        stall_interval_seconds=_env_hours("STALL_INTERVAL_HOURS", "30"),
        interval_seconds=_env_hours("FARM_INTERVAL_HOURS", "12"),
        startup_delay_seconds=_env_int("STARTUP_DELAY_SECONDS", "30"),
        between_accounts_seconds=_env_int("BETWEEN_ACCOUNTS_SECONDS", "20"),
        database_path=str(Path(data_dir) / "farm.db"),
    )
    numeric = {
        "CHAT_ACCOUNT_LIMIT": config.chat_account_limit,
        "BONUS_INTERVAL_HOURS": config.bonus_interval_seconds,
        "BONUS_RETRY_MINUTES": config.bonus_retry_seconds,
        "STALL_INTERVAL_HOURS": config.stall_interval_seconds,
        "FARM_INTERVAL_HOURS": config.interval_seconds,
        "STARTUP_DELAY_SECONDS": config.startup_delay_seconds,
        "BETWEEN_ACCOUNTS_SECONDS": config.between_accounts_seconds,
        "PAYOUT_THRESHOLD": config.payout_threshold,
        "PAYOUT_MAX_AMOUNT": config.payout_max_amount,
    }
    invalid = [name for name, value in numeric.items() if value <= 0]
    if invalid:
        raise RuntimeError(f"Значения должны быть больше нуля: {', '.join(invalid)}")
    if config.payout_enabled and config.payout_threshold > config.payout_max_amount:
        raise RuntimeError("PAYOUT_THRESHOLD не может быть больше PAYOUT_MAX_AMOUNT")
    return config
