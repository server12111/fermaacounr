from __future__ import annotations

import zipfile
from pathlib import Path


MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000


def extract_tdata_archive(archive: Path, destination: Path) -> Path:
    """Safely extract a Telegram Desktop tdata ZIP and return its directory."""
    extract_tdata_archive_contents(archive, destination)
    candidates = _find_tdata_directories(destination)
    if not candidates:
        raise ValueError("в ZIP не найдена папка tdata с файлом key_data")
    if len(candidates) > 1:
        raise ValueError("в ZIP найдено несколько папок tdata, оставьте одну")
    return candidates[0]


def extract_tdata_batch(archive: Path, destination: Path) -> list[tuple[str, Path]]:
    """Accept one tdata ZIP or an outer ZIP containing many tdata ZIP files."""
    outer = destination / "outer"
    extract_tdata_archive_contents(archive, outer)

    direct = _find_tdata_directories(outer)
    nested_archives = sorted(
        path for path in outer.rglob("*.zip") if path.is_file()
    )
    if direct and nested_archives:
        raise ValueError("смешанный архив: оставьте либо папки tdata, либо вложенные ZIP")

    if direct:
        return [(path.parent.name if path.name.casefold() == "tdata" else path.name, path) for path in direct]
    if not nested_archives:
        raise ValueError("не найдены папки tdata или вложенные ZIP-архивы")
    if len(nested_archives) > 100:
        raise ValueError("за один раз можно импортировать не больше 100 аккаунтов")

    total_nested_size = 0
    for nested in nested_archives:
        try:
            with zipfile.ZipFile(nested) as source:
                total_nested_size += sum(item.file_size for item in source.infolist())
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{nested.name}: повреждённый вложенный ZIP") from exc
        if total_nested_size > MAX_EXTRACTED_BYTES:
            raise ValueError("суммарный размер распакованных tdata больше 200 МБ")

    result: list[tuple[str, Path]] = []
    for index, nested in enumerate(nested_archives, start=1):
        target = destination / "accounts" / str(index)
        try:
            path = extract_tdata_archive(nested, target)
        except ValueError as exc:
            raise ValueError(f"{nested.name}: {exc}") from exc
        result.append((nested.stem, path))
    return result


def extract_tdata_archive_contents(archive: Path, destination: Path) -> None:
    """Safely extract a bounded ZIP without requiring tdata inside it."""
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("архив больше 50 МБ")
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("в архиве слишком много файлов")
            if sum(item.file_size for item in members) > MAX_EXTRACTED_BYTES:
                raise ValueError("распакованный архив больше 200 МБ")
            root = destination.resolve()
            for item in members:
                target = (destination / item.filename).resolve()
                if target != root and root not in target.parents:
                    raise ValueError("в архиве обнаружен небезопасный путь")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as reader, target.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        writer.write(chunk)
    except zipfile.BadZipFile as exc:
        raise ValueError("файл не является исправным ZIP-архивом") from exc


def _find_tdata_directories(destination: Path) -> list[Path]:
    return sorted({
        path.parent for path in destination.rglob("key_data")
        if path.is_file() and (path.parent.name.casefold() == "tdata" or path.parent == destination)
    })


async def tdata_to_string_session(tdata_path: Path):
    """Convert one unprotected Telegram Desktop account to a Telethon StringSession."""
    try:
        from opentele.api import UseCurrentSession
        from opentele.td import TDesktop
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError("не установлена зависимость opentele") from exc

    desktop = TDesktop(str(tdata_path))
    if not desktop.isLoaded():
        raise ValueError(
            "tdata не открылась: проверьте архив и отключите локальный пароль Telegram Desktop"
        )

    client = await desktop.ToTelethon(session=StringSession(), flag=UseCurrentSession)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("сессия tdata завершена или не авторизована")
        me = await client.get_me()
        return client.session.save(), me
    finally:
        await client.disconnect()
