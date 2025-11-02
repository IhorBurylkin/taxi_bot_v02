import asyncio
import contextlib
import os
import signal
import sys
import traceback
import tracemalloc

import sysmon
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
)

from bot_instance import initialize_bots
from config.config_from_db import ensure_config_exists
from db.db_table_init import close_pool, create_pool, init_db_tables, monitor_pool_health
from log.log import log_info, set_info_bot
from log.server_logs_scheduler import send_server_logs_once, start_daily_server_logs_task
from handlers import commands, verification, support
from web.web_app import start_server

_autosave_task: asyncio.Task | None = None
_sysmon_stop = None
_sysmon_thread = None


async def set_commands(bot) -> None:
    """Полная очистка и установка команд (ru/uk/en/de) для Default, Private, Group."""
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ]
    langs = [None, "ru", "uk", "en", "de"]

    # Полная очистка
    for scope in scopes:
        for lang in langs:
            with contextlib.suppress(Exception):
                if lang is None:
                    await bot.delete_my_commands(scope=scope)
                else:
                    await bot.delete_my_commands(scope=scope, language_code=lang)

    cmds = {
        "en": [
            BotCommand(command="start", description="Launch the bot"),
            BotCommand(command="help", description="Info on working with the bot"),
            BotCommand(command="support", description="Contact support"),
        ],
        "ru": [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Как пользоваться ботом"),
            BotCommand(command="support", description="Связаться с поддержкой"),
        ],
        "uk": [
            BotCommand(command="start", description="Запустити бота"),
            BotCommand(command="help", description="Як користуватися ботом"),
            BotCommand(command="support", description="Зв'язатися з підтримкою"),
        ],
        "de": [
            BotCommand(command="start", description="Bot starten"),
            BotCommand(command="help", description="Hilfe & Anleitung"),
            BotCommand(command="support", description="Support kontaktieren"),
        ],
    }

    for scope in scopes:
        for lang, commands in cmds.items():
            await bot.set_my_commands(commands, scope=scope, language_code=lang)

    # Фолбэк
    await bot.set_my_commands(cmds["en"], scope=BotCommandScopeDefault())


@contextlib.asynccontextmanager
async def database_connection(bot, info_bot):
    try:
        await create_pool()
        yield
    finally:
        with contextlib.suppress(Exception):
            await close_pool()
        # Закрываем сессии ботов
        for b in (bot, info_bot):
            if hasattr(b, "session"):
                with contextlib.suppress(Exception):
                    await b.session.close()


async def on_startup_bot(bot) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands(bot)


async def _autosave_loop() -> None:
    while True:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            with contextlib.suppress(Exception):
                await log_info(f"autosave failed: {e}", type_msg="warn")


async def main() -> None:
    global _autosave_task, _sysmon_stop, _sysmon_thread

    tracemalloc.start()

    # Системный монитор — только в TTY и если не отключен переменной окружения
    # if sys.stdout.isatty() and os.environ.get("SYSMON", "1") != "0":
    #     _sysmon_stop, _sysmon_thread = sysmon.start_in_thread(interval=1.0, use_colors=True)

    try:
        # 1) Боты
        bot, dp, info_bot, info_dp = await initialize_bots()
        set_info_bot(info_bot)
        support.set_main_bot(bot)
        dp.include_router(commands.router)
        dp.include_router(support.router)

        info_dp.include_router(verification.router)
        info_dp.include_router(support.admin_router)

        loop = asyncio.get_running_loop()
        shutdown_event: asyncio.Event = asyncio.Event()

        # Кроссплатформенная подписка на сигналы
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.add_signal_handler(sig, shutdown_event.set)

        # 2) DB + запуск поллинга
        async with database_connection(bot, info_bot):
            try:
                await init_db_tables()
                await log_info("Инициализация базы данных завершена", type_msg="info")
                await ensure_config_exists()

                pool_monitor_task = asyncio.create_task(monitor_pool_health())
                await log_info("Pool мониторинг запущен", type_msg="info")

                with contextlib.suppress(Exception):
                    _autosave_task = asyncio.create_task(_autosave_loop())

                ng_server = None
                ng_task = None
                try:
                    ng_server, ng_task = await start_server()  # host/port берём из env NG_PORT (по умолчанию 8080)
                    await log_info("Веб-панель (NiceGUI) запущена", type_msg="info")
                except Exception as e:
                    await log_info(f"Не удалось запустить NiceGUI: {e}", type_msg="warning")

                poll_main = asyncio.create_task(
                    dp.start_polling(
                        bot,
                        on_startup=lambda: on_startup_bot(bot),
                        handle_signals=False,
                        close_bot_session=False,
                    )
                )
                poll_info = asyncio.create_task(
                    info_dp.start_polling(
                        info_bot,
                        on_startup=lambda: on_startup_bot(info_bot),
                        handle_signals=False,
                        close_bot_session=False,
                    )
                )

                # Отчёты по логам
                # await send_server_logs_once()
                asyncio.create_task(start_daily_server_logs_task())

                await log_info("Все сервисы запущены успешно.", type_msg="info")

                await shutdown_event.wait()
                #await log_info("Получен сигнал завершения, начинаем graceful shutdown...", type_msg="info")

                pool_monitor_task.cancel()
                try:
                    await pool_monitor_task
                except asyncio.CancelledError:
                    pass
                
                # 1. Остановить приём новых запросов
                await dp.stop_polling()
                await info_dp.stop_polling()
                await log_info("Polling остановлен", type_msg="info")
                
                # 2. Дать время завершить текущие запросы
                await asyncio.sleep(2.0)
                
                # 3. Закрыть веб-сервер
                if ng_server:
                    ng_server.should_exit = True
                    await asyncio.wait_for(ng_task, timeout=10.0)
                await log_info("Web-сервер остановлен", type_msg="info")
                
                # 4. Сохранить состояние
                if _autosave_task and not _autosave_task.done():
                    _autosave_task.cancel()
                    try:
                        await _autosave_task
                    except asyncio.CancelledError:
                        pass
                await log_info("Autosave завершён", type_msg="info")
                
                # 5. Закрыть все активные соединения
                await asyncio.gather(poll_main, poll_info, return_exceptions=True)
                await log_info("Все задачи завершены", type_msg="info")
                
                # 6. Отправить финальные логи
                # try:
                #     await send_server_logs_once()
                # except Exception as e:
                #     print(f"Ошибка отправки финальных логов: {e}", file=sys.stderr)
                
            except Exception as e:
                await log_info(f"Ошибка shutdown: {e}", type_msg="error")
                raise
    finally:
        if _sysmon_stop:
            _sysmon_stop.set()
            if _sysmon_thread:
                _sysmon_thread.join(timeout=0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Фатальная ошибка: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
    else:
        sys.exit(0)
