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
    farm_command: str
    farm_enabled: bool
    bonus_chat: str
    bonus_command: str
    bonus_bot_username: str
    bonus_extra_seconds: int
    stall_command: str
    stall_button: str
    stall_interval_seconds: int
    interval_seconds: int
    startup_delay_seconds: int
    between_accounts_seconds: int
    database_path: str = "data/farm.db"


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

    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=admin_ids,
        api_id=api_id,
        api_hash=os.environ["TG_API_HASH"],
        session_secret=os.environ["SESSION_SECRET"],
        target_chat=os.getenv("TARGET_CHAT", "@VirusikChat"),
        farm_command=os.getenv("FARM_COMMAND", "ферма"),
        farm_enabled=os.getenv("FARM_ENABLED", "false").casefold() in {"1", "true", "yes", "on"},
        bonus_chat=os.getenv("BONUS_CHAT", "https://t.me/piar_grames"),
        bonus_command=os.getenv("BONUS_COMMAND", "б"),
        bonus_bot_username=os.getenv("BONUS_BOT_USERNAME", "valyutaTG_bot").lstrip("@").casefold(),
        bonus_extra_seconds=int(os.getenv("BONUS_EXTRA_MINUTES", "5")) * 60,
        stall_command=os.getenv("STALL_COMMAND", "ларек"),
        stall_button=os.getenv("STALL_BUTTON", "собрать"),
        stall_interval_seconds=int(float(os.getenv("STALL_INTERVAL_HOURS", "30")) * 3600),
        interval_seconds=int(float(os.getenv("FARM_INTERVAL_HOURS", "10")) * 3600),
        startup_delay_seconds=int(os.getenv("STARTUP_DELAY_SECONDS", "30")),
        between_accounts_seconds=int(os.getenv("BETWEEN_ACCOUNTS_SECONDS", "20")),
        database_path=str(Path(data_dir) / "farm.db"),
    )
