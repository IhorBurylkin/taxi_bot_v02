import logging
import asyncio
import os
from aiogram.types import InlineKeyboardMarkup, FSInputFile, InputMediaPhoto, InputMediaDocument
from config.config import (
    LOGGING_FILE_PATH,
    LOGGING_SETTINGS_TO_SEND_ERRORS,
    LOGGING_SETTINGS_TO_SEND,
    LOGGING_SETTINGS_TO_SEND_NEW_USERS,
    LOGGING_SETTINGS_TO_SEND_ORDERS,
    LOGGING_SETTINGS_TO_SEND_SUPPORT,
    LOGGING_FILE_PATH_ADMINS,
)
_initialized = False
_info_bot = None
_admin_logger = None

async def init_logging():
    global _initialized
    if _initialized:
        return

    await asyncio.sleep(0)  # To maintain asynchronous interface

    # Ensure log directory exists
    log_dir = os.path.dirname(LOGGING_FILE_PATH)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOGGING_FILE_PATH, encoding="utf-8")
        ]
    )

    _initialized = True

async def init_admin_logging():
    """Отдельный логгер, пишущий в LOGGING_FILE_PATH_ADMINS."""
    global _admin_logger
    if _admin_logger is not None:
        return

    await asyncio.sleep(0)

    # гарантируем, что папка существует
    admin_log_dir = os.path.dirname(LOGGING_FILE_PATH_ADMINS)
    if admin_log_dir and not os.path.exists(admin_log_dir):
        os.makedirs(admin_log_dir, exist_ok=True)

    logger = logging.getLogger("admins")
    logger.setLevel(logging.INFO)

    # избегаем дублей хендлеров
    need_file = True
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                # сравним абсолютные пути
                if os.path.abspath(h.baseFilename) == os.path.abspath(LOGGING_FILE_PATH_ADMINS):
                    need_file = False
                    break
            except Exception:
                pass

    if need_file:
        fh = logging.FileHandler(LOGGING_FILE_PATH_ADMINS, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    # чтобы записи не улетали ещё и в root-логгер
    logger.propagate = False
    _admin_logger = logger

def set_info_bot(bot):
    """Set the info bot instance to use for logging"""
    global _info_bot
    _info_bot = bot


async def send_info_msg(
    text=None,
    message_thread_id=None,
    info_bot=None,
    chat_id=None,
    type_msg_tg=None,
    log=None,
    *,
    photo: str | list[str] | None = None,
    document: str | list[str] | None = None,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None
):

    """
    Унифицированная отправка служебных сообщений.
    - text: обычное сообщение (можно с reply_markup)
    - photo|document: строка ИЛИ список. Если список длиной >1 → отправка альбома (caption у первого элемента).
      ВНИМАНИЕ: reply_markup у альбома не поддерживается Bot API → если передан, сначала шлём text+клавиатуру.
    """
    await init_logging()
    logger = logging.getLogger()  # root по умолчанию
    if log == "admins":
        await init_admin_logging()
        logger = _admin_logger or logger

    def _is_http_url(s: str) -> bool:
        return isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))

    def _as_inputfile(val):
        """HTTP(S) -> как есть; существующий локальный путь -> FSInputFile; иначе (в т.ч. file_id) -> как есть."""
        if isinstance(val, str):
            if _is_http_url(val):
                return val
            if os.path.exists(val):
                return FSInputFile(val)
        return val

    # 1) Выбираем набор настроек по типу
    settings = LOGGING_SETTINGS_TO_SEND           # дефолт
    if type_msg_tg == "error":
        settings = LOGGING_SETTINGS_TO_SEND_ERRORS
    elif type_msg_tg == "new_users":
        settings = LOGGING_SETTINGS_TO_SEND_NEW_USERS
    elif type_msg_tg == "orders":
        settings = LOGGING_SETTINGS_TO_SEND_ORDERS
    elif type_msg_tg == "support":
        settings = LOGGING_SETTINGS_TO_SEND_SUPPORT
    elif type_msg_tg == "warning":
        settings = LOGGING_SETTINGS_TO_SEND_ERRORS

    # Если запрещено — выходим тихо
    if not settings.get("permission", True):
        return

    # 2) Целевой чат/тред
    target_chat_id = chat_id if chat_id is not None else settings.get("chat_id")
    target_thread_id = message_thread_id if message_thread_id is not None else settings.get("message_thread_id")

    # Приведём chat_id к int (часто в .env это строка)
    try:
        target_chat_id = int(str(target_chat_id))
    except Exception:
        pass  # если всё-таки строковый @username — Telegram сам разберётся

    # 3) Бот
    bot = info_bot if info_bot is not None else _info_bot
    if bot is None:
        logger.warning("send_info_msg: info_bot не инициализирован — сообщение не отправлено")
        return

    # 4) Подготовка получателя
    try:
        kwargs = dict(chat_id=target_chat_id)
        if target_thread_id not in (None, 0):
            kwargs["message_thread_id"] = target_thread_id

        # 5) Нормализуем вход: одиночное значение -> список; пустоты выкидываем
        photos: list[str] = []
        docs:   list[str] = []
        if isinstance(photo, (list, tuple)):
            photos = [p for p in photo if p]
        elif photo:
            photos = [photo]
        if isinstance(document, (list, tuple)):
            docs = [d for d in document if d]
        elif document:
            docs = [document]

        # определяем режим: сначала фото, если они есть; иначе документы; иначе текст
        use_photos = len(photos) > 0
        use_docs   = (not use_photos) and (len(docs) > 0)

        # 6) Если альбом и есть клавиатура — сначала шлём текст с клавиатурой
        if (use_photos and len(photos) > 1) or (use_docs and len(docs) > 1):
            if reply_markup is not None:
                lead_text = text if text is not None else (caption or "")
                await bot.send_message(text=lead_text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs)
                # для альбома клавиатуру в сам альбом прикрепить нельзя (ограничение Bot API)
                reply_markup = None

        # 7) Отправка по режимам
        if use_photos:
            if len(photos) == 1:
                resp = await bot.send_photo(
                    photo=_as_inputfile(photos[0]),
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    **kwargs
                )
            else:
                media = []
                for i, p in enumerate(photos):
                    media.append(
                        InputMediaPhoto(
                            media=_as_inputfile(p),
                            caption=(caption if (i == 0 and caption) else None),
                            parse_mode=(parse_mode if (i == 0 and caption and parse_mode) else None),
                        )
                    )
                resp = await bot.send_media_group(media=media, **kwargs)
        elif use_docs:
            if len(docs) == 1:
                resp = await bot.send_document(
                    document=_as_inputfile(docs[0]),
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    **kwargs
                )
            else:
                media = []
                for i, d in enumerate(docs):
                    media.append(
                        InputMediaDocument(
                            media=_as_inputfile(d),
                            caption=(caption if (i == 0 and caption) else None),
                            parse_mode=(parse_mode if (i == 0 and caption and parse_mode) else None),
                        )
                    )
                resp = await bot.send_media_group(media=media, **kwargs)
        else:
            # чисто текст
            if text is None:
                text = ""
            resp = await bot.send_message(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs
            )

            media_label = (
                f"photo[{len(photos)}]" if use_photos else
                f"document[{len(docs)}]" if use_docs else
                "text"
            )
            logger.info(f"send_info_msg OK → chat={target_chat_id}, thread={target_thread_id}, media={media_label}")

            return resp

    except Exception as e:
        logger.error(f"Ошибка отправки служебного сообщения → chat={target_chat_id}, thread={target_thread_id}: {e}")
        return

async def log_info(message: str, type_msg: str, log: str | None = None, *args, **kwargs: str):
    await init_logging()
    logger = logging.getLogger()  # root по умолчанию
    if log == "admins":
        await init_admin_logging()
        logger = _admin_logger or logger
    if "info" in type_msg:  
        logger.info(message, *args, extra=kwargs)
    elif "error" in type_msg:
        logger.error(message, *args, extra=kwargs)
        await send_info_msg(
            text=f'Тип сообщения: Ошибка\n{message}\n{args}\n{kwargs}', 
            message_thread_id=LOGGING_SETTINGS_TO_SEND_ERRORS["message_thread_id"],
            info_bot=_info_bot,
            type_msg_tg="error"
        )
    elif "warning" in type_msg:
        logger.warning(message, *args, extra=kwargs)
        await send_info_msg(
            text=f'Тип сообщения: Предупреждение\n{message}\n{args}\n{kwargs}', 
            message_thread_id=LOGGING_SETTINGS_TO_SEND_ERRORS["message_thread_id"],
            info_bot=_info_bot,
            type_msg_tg="warning"
        )