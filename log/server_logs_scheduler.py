# -*- coding: utf-8 -*-
"""
server_logs_scheduler.py — ежедневная отправка серверных логов в служебный чат и ротация файлов

Функционал:
- Ежедневно в 07:00 UTC отправляет содержимое LOGGING_FILE_PATH и LOGGING_FILE_PATH_ADMINS
  в чат, заданный в LOGGING_SETTINGS_TO_SEND_SERVER_LOGS.
- При успешной отправке каждого файла — удаляет его и переинициализирует файловые хендлеры,
  чтобы начать запись с нуля.
- Можно вызвать разово `send_server_logs_once()` (например, для теста).

Интеграция:
1) В точке старта бота вызовите set_info_bot(bot) из вашего log.py (у вас уже есть эта функция).
2) Запустите фоновой таск:
       from server_logs_scheduler import start_daily_server_logs_task
       asyncio.create_task(start_daily_server_logs_task())
   или используйте `await start_daily_server_logs_task()` в фоне вашего приложения.

Зависимости: использует функции/переменные из log.py и конфиг из config.config.
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timedelta, timezone
import logging


from config.config import (
    LOGGING_FILE_PATH,
    LOGGING_FILE_PATH_ADMINS,
    LOGGING_SETTINGS_TO_SEND_SERVER_LOGS,
)
# Берём ваши вспомогательные функции и глобалы
from log.log import send_info_msg, log_info, init_logging, init_admin_logging
from aiogram.types import FSInputFile

# ---------------------------------------------------------------------------
# Вспомогательные утилиты
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _seconds_until_next(hour: int = 7, minute: int = 0, second: int = 0) -> float:
    """Секунды до ближайшего запуска в указанное время UTC (по умолчанию 07:00:00)."""
    now = _now_utc()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _is_nonempty_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:
        return False

def _file_caption(path: str) -> str:
    try:
        sz = os.path.getsize(path)
    except Exception:
        sz = 0
    ts = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    base = os.path.basename(path)
    return f"📄 {base} • {sz} bytes • dump at {ts}"

# ---------------------------------------------------------------------------
# Ротация файлов: закрыть file handlers, удалить файл, заново повесить handler
# ---------------------------------------------------------------------------

def _close_handlers_for_file(target_path: str, logger: logging.Logger) -> None:
    """Удаляет FileHandler-ы у logger, которые пишут в target_path."""
    to_remove = []
    target_abs = os.path.abspath(target_path)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                if os.path.abspath(h.baseFilename) == target_abs:
                    h.flush()
                    h.close()
                    to_remove.append(h)
            except Exception:
                # на всякий пожарный всё равно удалим
                to_remove.append(h)
    for h in to_remove:
        try:
            logger.removeHandler(h)
        except Exception:
            pass

def _readd_file_handler(path: str, logger: logging.Logger) -> None:
    _ensure_dir(path)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

def _get_admin_logger() -> logging.Logger | None:
    # Ваш admin-логгер создаётся в init_admin_logging() как logger "admins"
    try:
        return logging.getLogger("admins")
    except Exception:
        return None

def _rotate_file(path: str, logger: logging.Logger) -> None:
    """Закрыть handlers для path, удалить файл, повесить свежий FileHandler."""
    _close_handlers_for_file(path, logger)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    _readd_file_handler(path, logger)


async def _send_log_file(
    *,
    file_path: str,
    logger: logging.Logger,
    chat_id: int | str | None,
    thread_id: int | None,
) -> bool:
    """Отправляет указанный лог-файл и при успехе выполняет ротацию handlers."""
    try:
        if not _is_nonempty_file(file_path):
            await log_info(
                f"server_logs: файл пуст или отсутствует → {file_path}",
                type_msg="info",
            )
            return False

        try:
            response = await send_info_msg(
                document=FSInputFile(file_path),
                caption=_file_caption(file_path),
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
        except Exception as send_error:  # noqa: BLE001
            await log_info(
                f"server_logs: ошибка при отправке файла {file_path}: {send_error!r}",
                type_msg="error",
            )
            return False

        if response is None:
            await log_info(
                f"server_logs: бот не подтвердил доставку файла → {file_path}",
                type_msg="warning",
            )
            return False

        await log_info(
            f"server_logs: файл успешно отправлен → {file_path}",
            type_msg="info",
        )
        _rotate_file(file_path, logger)
        await log_info(
            f"server_logs: файл переинициализирован после отправки → {file_path}",
            type_msg="info",
        )
        return True
    except Exception as unexpected_error:  # noqa: BLE001
        await log_info(
            f"server_logs: критическая ошибка обработки файла {file_path}: {unexpected_error!r}",
            type_msg="error",
        )
        return False

# ---------------------------------------------------------------------------
# Разовая отправка
# ---------------------------------------------------------------------------

async def send_server_logs_once() -> None:
    """
    Разовая отправка файлов LOGGING_FILE_PATH и LOGGING_FILE_PATH_ADMINS
    в чат LOGGING_SETTINGS_TO_SEND_SERVER_LOGS. Успех по каждому файлу — отдельный.
    После успешной отправки файла: удалить и переинициализировать соответствующий хендлер.
    """
    # Если запрещено — просто выходим
    if not LOGGING_SETTINGS_TO_SEND_SERVER_LOGS.get("permission", True):
        await log_info("server_logs: отправка отключена настройками", type_msg="info")
        return

    chat_id = LOGGING_SETTINGS_TO_SEND_SERVER_LOGS.get("chat_id")
    thread_id = LOGGING_SETTINGS_TO_SEND_SERVER_LOGS.get("message_thread_id")

    # Приведём chat_id к int, если это ID; если это username — Telegram сам поймёт
    try:
        chat_id_cast = int(str(chat_id))
    except Exception:
        chat_id_cast = chat_id

    # Убедимся, что логгеры инициализированы
    await init_logging()
    await init_admin_logging()
    root_logger = logging.getLogger()
    admin_logger = _get_admin_logger() or logging.getLogger()

    await _send_log_file(
        file_path=LOGGING_FILE_PATH,
        logger=root_logger,
        chat_id=chat_id_cast,
        thread_id=thread_id,
    )

    await _send_log_file(
        file_path=LOGGING_FILE_PATH_ADMINS,
        logger=admin_logger,
        chat_id=chat_id_cast,
        thread_id=thread_id,
    )

# ---------------------------------------------------------------------------
# Бесконечный ежедневный таск
# ---------------------------------------------------------------------------

async def start_daily_server_logs_task():
    """
    Фоновый таск: ждёт до ближайших 07:00 UTC, затем каждый день отправляет логи.
    Безопасен к исключениям — не падает, пишет в лог и продолжает.
    """
    while True:
        try:
            wait_sec = _seconds_until_next(7, 0, 0)
            await asyncio.sleep(wait_sec)
            await send_server_logs_once()
            # на случай, если запуск занял время — следующий запуск через 24 часа
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await log_info(f"server_logs: ошибка фонового цикла: {e}", type_msg="error")
            except Exception:
                pass
            # не крутимся слишком быстро при ошибках
            await asyncio.sleep(60)
