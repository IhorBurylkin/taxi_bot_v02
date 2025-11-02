# -*- coding: utf-8 -*-
"""
support_clean.py — обработчики техподдержки

Цели рефакторинга:
- Чёткая структура, докстринги, единый стиль логирования.
- Без изменения внешнего API: совместимы callback_data, команда /support,
  а также программный вызов `cmd_support(message=None, state=..., user=..., chat_id=..., bot=...)`.
- Аккуратная работа с интро-сообщением и состояниями.
- Унифицированная отправка в служебный чат (копирование фото/доков, текстовые заглушки).
- Осторожная обработка caption-длины (усечение при необходимости) и трёхуровневый поиск user_id в reply-цепочке.

Секции:
    0) Импорты и константы
    1) FSM состояния
    2) Вспомогательные функции
    3) /support: запуск с интро и кнопкой "Отмена"
    4) Отмена ожидания
    5) Сбор обращений: текст / фото / документ
    6) Ответ админа из служебного чата пользователю (по reply)
    7) Игнорирование обычных сообщений в служебном чате
"""

from __future__ import annotations

import re
import json
import tempfile
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Optional, Any, Dict, List, Literal

from aiogram import F, Router, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, CallbackQuery, User, FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config.config import (
    LOGGING_SETTINGS_TO_SEND_SUPPORT,
    DEFAULT_LANGUAGES,
    MESSAGES,
    USERS_TABLE,
)
from log.log import log_info, send_info_msg
from db.db_utils import get_user_data, append_support_message
from web.web_notify import notify_user
from keyboards.inline_kb_support import cancel_support_keyboard

# ----------------------------------------------------------------------------
# 0) Импорты и константы
# ----------------------------------------------------------------------------

# Основной роутер для пользовательских сценариев
router = Router(name="support_user")
# Отдельный роутер для админских событий (используется info-bot)
admin_router = Router(name="support_admin")

_main_bot: Bot | None = None


def set_main_bot(bot: Bot | None) -> None:
    """Сохраняем ссылку на основной бот для ответов пользователям."""
    global _main_bot
    _main_bot = bot

SUPPORT_CHAT_ID: int = int(LOGGING_SETTINGS_TO_SEND_SUPPORT.get("chat_id", 0))
# Приводим ID топика к int, если его положили строкой в конфиг
raw_thread = LOGGING_SETTINGS_TO_SEND_SUPPORT.get("message_thread_id")
SUPPORT_THREAD_ID = int(raw_thread) if isinstance(raw_thread, str) and raw_thread.isdigit() else raw_thread

# Лимиты Telegram (актуальные на момент написания)
CAPTION_LIMIT = 1024   # подпись к медиа
REPLY_CHAIN_MAX_DEPTH = 10


# ----------------------------------------------------------------------------
# 1) FSM состояния
# ----------------------------------------------------------------------------

class SupportStates(StatesGroup):
    waiting = State()


# ----------------------------------------------------------------------------
# 2) Вспомогательные функции
# ----------------------------------------------------------------------------

async def user_lang(user_id: int, fallback: str = DEFAULT_LANGUAGES) -> str:
    """Вернуть язык пользователя или fallback."""
    user = await get_user_data(USERS_TABLE, user_id)
    return (user or {}).get("language") or fallback


def _msgs(lang: str) -> dict:
    """Короткий доступ к MESSAGES с запасным вариантом."""
    return MESSAGES.get(lang) or MESSAGES.get(DEFAULT_LANGUAGES) or {}


def _extract_user_id_from_support_stub(text_or_caption: str | None) -> int | None:
    """
    Ищет шаблоны "User: 12345" или "user_id=12345" в тексте/подписи.
    Возвращает user_id или None.
    """
    if not text_or_caption:
        return None
    m = re.search(r"(?:User\s*:\s*|user_id\s*=\s*)(\d+)", text_or_caption)
    return int(m.group(1)) if m else None


def _role_bracket(user_row: dict | None) -> str:
    """Красивый префикс роли в заголовке, по умолчанию Passenger."""
    role = (user_row or {}).get("role", "") or ""
    r = str(role).lower()
    return "[Support/Driver]" if "driver" in r else "[Support/Passenger]"


def _compose_header(user: User, user_row: dict | None, text_for_header: str | None) -> str:
    """Собрать заголовок-заглушку для служебного чата (используется как caption/сообщение)."""
    header = (
        f"{_role_bracket(user_row)}\n"
        f"User: {user.id}\n"
        f"Username: {('@' + user.username) if user.username else 'None'}\n"
        f"First_name: {user.first_name or 'None'}"
    )
    if text_for_header:
        header = f"{header}\nText:\n{text_for_header}"
    return header


def _truncate(s: str, limit: int) -> str:
    """Аккуратно усечь строку до limit, добавив многоточие при необходимости."""
    if s is None:
        return s
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _now_utc_iso() -> str:
    """Возвращает текущее время в формате ISO8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _extract_message_attachments(message: Message) -> List[Dict[str, Any]]:
    """Формирует перечень вложений для сохранения в истории переписки."""
    attachments: List[Dict[str, Any]] = []

    photo_set = getattr(message, "photo", None) or []
    if photo_set:
        photo = photo_set[-1]
        attachments.append(
            {
                "kind": "photo",
                "file_id": getattr(photo, "file_id", None),
                "file_unique_id": getattr(photo, "file_unique_id", None),
                "file_size": getattr(photo, "file_size", None),
                "width": getattr(photo, "width", None),
                "height": getattr(photo, "height", None),
            }
        )

    document = getattr(message, "document", None)
    if document is not None:
        attachments.append(
            {
                "kind": "document",
                "file_id": getattr(document, "file_id", None),
                "file_unique_id": getattr(document, "file_unique_id", None),
                "file_name": getattr(document, "file_name", None),
                "mime_type": getattr(document, "mime_type", None),
                "file_size": getattr(document, "file_size", None),
            }
        )

    audio = getattr(message, "audio", None)
    if audio is not None:
        attachments.append(
            {
                "kind": "audio",
                "file_id": getattr(audio, "file_id", None),
                "file_unique_id": getattr(audio, "file_unique_id", None),
                "file_name": getattr(audio, "file_name", None),
                "mime_type": getattr(audio, "mime_type", None),
                "duration": getattr(audio, "duration", None),
                "file_size": getattr(audio, "file_size", None),
            }
        )

    voice = getattr(message, "voice", None)
    if voice is not None:
        attachments.append(
            {
                "kind": "voice",
                "file_id": getattr(voice, "file_id", None),
                "file_unique_id": getattr(voice, "file_unique_id", None),
                "duration": getattr(voice, "duration", None),
                "file_size": getattr(voice, "file_size", None),
            }
        )

    video = getattr(message, "video", None)
    if video is not None:
        attachments.append(
            {
                "kind": "video",
                "file_id": getattr(video, "file_id", None),
                "file_unique_id": getattr(video, "file_unique_id", None),
                "duration": getattr(video, "duration", None),
                "width": getattr(video, "width", None),
                "height": getattr(video, "height", None),
                "file_size": getattr(video, "file_size", None),
            }
        )

    return attachments


def _compose_support_entry(
    *,
    author: Literal["user", "admin"],
    text: str | None,
    attachments: List[Dict[str, Any]],
    source: str,
    message: Message,
) -> Dict[str, Any]:
    """Готовит запись для сохранения в таблице support_requests."""
    reply = getattr(message, "reply_to_message", None)
    entry: Dict[str, Any] = {
        "id": uuid4().hex,
        "ts": _now_utc_iso(),
        "author": author,
        "text": (text or "").strip(),
        "attachments": attachments,
        "meta": {
            "source": source,
            "message_id": getattr(message, "message_id", None),
            "chat_id": getattr(getattr(message, "chat", None), "id", None),
            "reply_to": getattr(reply, "message_id", None),
        },
    }
    return entry


async def _store_support_entry(user_id: int, entry: Dict[str, Any]) -> None:
    """Сохраняет сообщение в истории переписки и логирует сбои."""
    try:
        author_raw = entry.get("author")
        author: Literal["user", "admin"] = "admin" if author_raw == "admin" else "user"
        await append_support_message(user_id, entry, author=author)
    except Exception as err:
        await log_info(
            f"Не удалось обновить историю поддержки: {err}",
            type_msg="error",
            user_id=user_id,
        )


def _resolve_info_bot(message: Message) -> Bot | None:
    """Возвращает экземпляр info-бота из контекста события."""
    candidate = getattr(message, "bot", None)
    if candidate is not None:
        return candidate
    conf = getattr(message, "conf", None)
    if isinstance(conf, dict):
        bot_from_conf = conf.get("bot")
        if isinstance(bot_from_conf, Bot):
            return bot_from_conf
    return None


async def _download_file_via_bot(bot: Bot, file_id: str, *, filename_hint: str | None = None) -> Path | None:
    """Скачивает файл во временную директорию и возвращает путь."""
    try:
        file_info = await bot.get_file(file_id)
        remote_path = getattr(file_info, "file_path", None) or ""
        suffix = ""
        if filename_hint:
            suffix = Path(filename_hint).suffix
        if not suffix and remote_path:
            suffix = Path(remote_path).suffix
        temp_dir = Path(tempfile.gettempdir())
        temp_dir.mkdir(parents=True, exist_ok=True)
        target_path = temp_dir / f"support_{uuid4().hex}{suffix}"
        await bot.download_file(file_path=remote_path, destination=target_path)
        return target_path
    except Exception as download_error:
        await log_info(
            f"[support_admin_reply] не удалось скачать файл: {download_error}",
            type_msg="error",
        )
        return None

# -- Отправка в служебный чат -------------------------------------------------

async def _send_support_entry(message: Message, user_row: dict | None, text_for_header: str | None):
    """
    Универсальная отправка обращения в служебный чат:
    - для фото/документа делаем copy_message (с заменой/установкой caption, если возможно),
      fallback — отправка "шапки" отдельным сообщением и raw-копирование без подписи.
    - для обычного текста — просто отправляем "шапку" через send_info_msg.
    """
    user = message.from_user
    header = _compose_header(user, user_row, text_for_header)

    # Фото (сжатое) — пробуем скопировать с новой подписью
    if message.photo:
        try:
            await message.bot.copy_message(
                chat_id=SUPPORT_CHAT_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=SUPPORT_THREAD_ID,
                caption=_truncate(header, CAPTION_LIMIT),
            )
        except TelegramBadRequest as e:
            # Если проблема с длиной caption — отправим шапку отдельным текстом и дублируем фото без подписи
            if "caption is too long" in str(e).lower():
                await send_info_msg(text=header, type_msg_tg="support")
                await message.bot.copy_message(
                    chat_id=SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=SUPPORT_THREAD_ID,
                )
            else:
                # Фолбэк — отправим через send_info_msg (старый путь)
                await send_info_msg(photo=message.photo[-1].file_id, caption=_truncate(header, CAPTION_LIMIT), type_msg_tg="support")
        except Exception:
            await send_info_msg(photo=message.photo[-1].file_id, caption=_truncate(header, CAPTION_LIMIT), type_msg_tg="support")
        return

    # Документ — аналогично
    if message.document:
        try:
            await message.bot.copy_message(
                chat_id=SUPPORT_CHAT_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=SUPPORT_THREAD_ID,
                caption=_truncate(header, CAPTION_LIMIT),
            )
        except TelegramBadRequest as e:
            if "caption is too long" in str(e).lower():
                await send_info_msg(text=header, type_msg_tg="support")
                await message.bot.copy_message(
                    chat_id=SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=SUPPORT_THREAD_ID,
                )
            else:
                await send_info_msg(document=message.document.file_id, caption=_truncate(header, CAPTION_LIMIT), type_msg_tg="support")
        except Exception:
            await send_info_msg(document=message.document.file_id, caption=_truncate(header, CAPTION_LIMIT), type_msg_tg="support")
        return

    # Обычный текст
    await send_info_msg(text=header, type_msg_tg="support")


# ----------------------------------------------------------------------------
# 3) /support (PRIVATE)
# ----------------------------------------------------------------------------

@router.message(F.chat.type == ChatType.PRIVATE, Command("support"))
async def cmd_support(
    message: Optional[Message] = None,
    state: Optional[FSMContext] = None,
    *,
    user: Optional[User] = None,
    chat_id: Optional[int] = None,
    bot: Optional[Bot] = None,
):
    """
    Точка входа: показывает интро, включает ожидание сообщения пользователя и
    прикрепляет inline-кнопку "Отмена". Может вызываться как хэндлером, так и программно.
    """
    if state is None:
        raise RuntimeError("cmd_support: FSM 'state' is required")

    # Пользователь
    if user is None:
        if message and message.from_user:
            user = message.from_user
        else:
            raise RuntimeError("cmd_support: user is not provided and message has no from_user")
    uid = user.id

    # Куда писать интро
    if chat_id is None:
        if message and message.chat and message.chat.type == "private":
            chat_id = message.chat.id
        else:
            chat_id = uid

    # Бот
    if bot is None:
        if message is not None:
            bot = message.bot
        else:
            raise RuntimeError("cmd_support: 'bot' is required when 'message' is None")

    # Текст интро
    lang = await user_lang(uid)
    intro = _msgs(lang).get("support_intro") or "🛟 Напишите сообщение для техподдержки…"

    # Стейт и интро
    await state.clear()
    await state.set_state(SupportStates.waiting)
    sent = await bot.send_message(chat_id, intro, reply_markup=cancel_support_keyboard(lang))
    await state.update_data(support_intro_msg_id=sent.message_id, support_user_id=uid)
    await log_info(f"/support initiated for user={uid}", type_msg="info")


# ----------------------------------------------------------------------------
# 4) Отмена ожидания
# ----------------------------------------------------------------------------

@router.callback_query(
    F.message.chat.type == ChatType.PRIVATE,
    StateFilter(SupportStates.waiting),
    F.data == "cancel_support",
)
async def support_cancel(callback: CallbackQuery, state: FSMContext):
    """Отменить ожидание сообщения для техподдержки и убрать интро."""
    try:
        data = await state.get_data()
        intro_id = data.get("support_intro_msg_id")

        if intro_id:
            try:
                await callback.bot.delete_message(callback.message.chat.id, intro_id)
            except Exception:
                pass

        lang = await user_lang(callback.from_user.id)
        await state.clear()
        await callback.answer()
        await log_info(f"Support waiting cancelled: user={callback.from_user.id}", type_msg="info")
        await callback.message.answer(_msgs(lang).get("closed_ok") or "Закрыто")
    except Exception as e:
        await log_info(f"Ошибка отмены ожидания техподдержки: {e}", type_msg="error")


# ----------------------------------------------------------------------------
# 5) Сбор обращений
# ----------------------------------------------------------------------------

@router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(SupportStates.waiting),
    F.text,
)
async def support_collect_text(message: Message, state: FSMContext):
    """Пользователь прислал текст — передать в служебный чат и подтвердить отправку."""
    try:
        data = await state.get_data()
        intro_id = data.get("support_intro_msg_id")
        if intro_id:
            try:
                await message.bot.delete_message(message.chat.id, intro_id)
            except Exception:
                pass

        user_row = await get_user_data(USERS_TABLE, message.from_user.id)
        clean_text = (message.text or "").strip()
        await _send_support_entry(message, user_row, text_for_header=clean_text)

        entry = _compose_support_entry(
            author="user",
            text=clean_text,
            attachments=_extract_message_attachments(message),
            source="telegram",
            message=message,
        )
        await _store_support_entry(message.from_user.id, entry)

        lang = await user_lang(message.from_user.id)
        await message.answer(_msgs(lang).get("support_sent") or "✅ Сообщение передано в техподдержку. Спасибо!")

        await state.clear()
        await log_info(f"Support message sent (text) by user={message.from_user.id}", type_msg="info")
    except Exception as e:
        await log_info(f"Ошибка отправки текстового сообщения в техподдержку: {e}", type_msg="error")


@router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(SupportStates.waiting),
    F.photo,
)
async def support_collect_photo(message: Message, state: FSMContext):
    """Пользователь прислал фото — передать в служебный чат (copy_message) и подтвердить отправку."""
    try:
        data = await state.get_data()
        intro_id = data.get("support_intro_msg_id")
        if intro_id:
            try:
                await message.bot.delete_message(message.chat.id, intro_id)
            except Exception:
                pass

        user_row = await get_user_data(USERS_TABLE, message.from_user.id)
        cap = (message.caption or "").strip() or None
        await _send_support_entry(message, user_row, text_for_header=cap)

        entry = _compose_support_entry(
            author="user",
            text=cap,
            attachments=_extract_message_attachments(message),
            source="telegram",
            message=message,
        )
        await _store_support_entry(message.from_user.id, entry)

        lang = await user_lang(message.from_user.id)
        await message.answer(_msgs(lang).get("support_sent") or "✅ Сообщение передано в техподдержку. Спасибо!")

        await state.clear()
        await log_info(f"Support message sent (photo) by user={message.from_user.id}", type_msg="info")
    except Exception as e:
        await log_info(f"Ошибка отправки фото в техподдержку: {e}", type_msg="error")


@router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(SupportStates.waiting),
    F.document,
)
async def support_collect_doc(message: Message, state: FSMContext):
    """Пользователь прислал документ — передать в служебный чат (copy_message) и подтвердить отправку."""
    try:
        data = await state.get_data()
        intro_id = data.get("support_intro_msg_id")
        if intro_id:
            try:
                await message.bot.delete_message(message.chat.id, intro_id)
            except Exception:
                pass

        user_row = await get_user_data(USERS_TABLE, message.from_user.id)
        cap = (message.caption or "").strip() or None
        await _send_support_entry(message, user_row, text_for_header=cap)

        entry = _compose_support_entry(
            author="user",
            text=cap,
            attachments=_extract_message_attachments(message),
            source="telegram",
            message=message,
        )
        await _store_support_entry(message.from_user.id, entry)

        lang = await user_lang(message.from_user.id)
        await message.answer(_msgs(lang).get("support_sent") or "✅ Сообщение передано в техподдержку. Спасибо!")

        await state.clear()
        await log_info(f"Support message sent (document) by user={message.from_user.id}", type_msg="info")
    except Exception as e:
        await log_info(f"Ошибка отправки документа в техподдержку: {e}", type_msg="error")


# ----------------------------------------------------------------------------
# 6) Ответ админа из группы → пользователю (по reply)
# ----------------------------------------------------------------------------

@admin_router.message(
    (F.chat.type.in_({ChatType.SUPERGROUP, ChatType.GROUP})) &
    (F.chat.id == SUPPORT_CHAT_ID) &
    (F.reply_to_message != None),
)
async def support_admin_reply(message: Message):
    """
    Админ в служебном чате отвечает (reply) на системное сообщение с заголовком —
    из текста/подписи извлекается user_id, и ответ отправляется пользователю.
    Для твит-супертопиков (topics) дополнительно проверяется соответствие thread id.
    """
    try:
        admin_id = getattr(message.from_user, "id", None)
        await log_info(
            f"[support_admin_reply] входящее сообщение reply_to={getattr(message.reply_to_message, 'message_id', None)} thread={getattr(message, 'message_thread_id', None)}",
            type_msg="info",
            user_id=admin_id,
        )
        if SUPPORT_THREAD_ID is not None:
            if getattr(message, "message_thread_id", None) != SUPPORT_THREAD_ID:
                # Другой топик — игнорируем молча
                await log_info(
                    "[support_admin_reply] пропуск из-за несоответствия thread_id",
                    type_msg="warning",
                    user_id=admin_id,
                )
                return

        # Поднимаемся по reply-цепочке и ищем user_id в шапке
        src = message.reply_to_message
        user_id = None
        depth = 0
        while src and depth < REPLY_CHAIN_MAX_DEPTH and not user_id:
            payload = (src.text or "") or (src.caption or "")
            user_id = _extract_user_id_from_support_stub(payload)
            await log_info(
                f"[support_admin_reply] проверка цепочки depth={depth} найден_uid={user_id}",
                type_msg="info",
                user_id=admin_id,
            )
            src = getattr(src, "reply_to_message", None)
            depth += 1

        if not user_id:
            # Нет id — просто игнорируем без шума
            await log_info(
                "[support_admin_reply] не удалось найти user_id в цепочке",
                type_msg="warning",
                user_id=admin_id,
            )
            return

        target_bot = _main_bot
        if not isinstance(target_bot, Bot):
            await log_info(
                "[support_admin_reply] основной бот недоступен для ответа",
                type_msg="error",
                user_id=admin_id,
            )
            return

        lang = await user_lang(user_id)
        msgs = _msgs(lang)

        # Текст
        if message.text and message.text.strip():
            txt = message.text.strip()
            reply_text_tpl = msgs.get("support_reply_text") or "🛟 Ответ техподдержки:\n\n{text}"
            reply_text = reply_text_tpl.format(text=txt)
            try:
                await target_bot.send_message(chat_id=user_id, text=reply_text)
                await log_info(f"Support reply delivered to user={user_id} (lang={lang})", type_msg="info")
            except TelegramForbiddenError:
                await log_info(
                    f"Не удалось отправить ответ пользователю {user_id}: бот заблокирован пользователем или отсутствует диалог.",
                    type_msg="error",
                )
            except Exception as e:
                await log_info(f"Ошибка отправки текстового ответа пользователю {user_id}: {e}", type_msg="error")
            else:
                entry = _compose_support_entry(
                    author="admin",
                    text=txt,
                    attachments=_extract_message_attachments(message),
                    source="admin_chat",
                    message=message,
                )
                await _store_support_entry(user_id, entry)

                toast = msgs.get("profile_support_new_reply_toast")
                if toast:
                    try:
                        await notify_user(user_id, toast, level="info", position="top")
                    except Exception as notify_error:
                        await log_info(
                            f"support_admin_reply: не удалось показать уведомление пользователю {user_id}: {notify_error}",
                            type_msg="warning",
                        )
                    await log_info(
                        f"[support_admin_reply] текстовый ответ сохранён для user_id={user_id}",
                        type_msg="info",
                        user_id=admin_id,
                    )
            return

        # Медиа/документ
        caption_text = (message.caption or "").strip() or None
        info_bot_instance = _resolve_info_bot(message)
        if info_bot_instance is None:
            await log_info(
                "[support_admin_reply] info-бот недоступен для скачивания вложений",
                type_msg="error",
                user_id=admin_id,
            )
            return

        temp_paths: list[Path] = []
        try:
            if message.document:
                doc = message.document
                temp_path = await _download_file_via_bot(
                    info_bot_instance,
                    doc.file_id,
                    filename_hint=getattr(doc, "file_name", None),
                )
                if temp_path is None:
                    return
                temp_paths.append(temp_path)
                await target_bot.send_document(
                    chat_id=user_id,
                    document=FSInputFile(temp_path),
                    caption=caption_text,
                )
            elif message.photo:
                photo = message.photo[-1]
                temp_path = await _download_file_via_bot(
                    info_bot_instance,
                    photo.file_id,
                    filename_hint=f"{photo.file_unique_id}.jpg",
                )
                if temp_path is None:
                    return
                temp_paths.append(temp_path)
                await target_bot.send_photo(
                    chat_id=user_id,
                    photo=FSInputFile(temp_path),
                    caption=caption_text,
                )
            elif message.video:
                video = message.video
                temp_path = await _download_file_via_bot(
                    info_bot_instance,
                    video.file_id,
                    filename_hint=getattr(video, "file_name", None),
                )
                if temp_path is None:
                    return
                temp_paths.append(temp_path)
                await target_bot.send_video(
                    chat_id=user_id,
                    video=FSInputFile(temp_path),
                    caption=caption_text,
                )
            elif message.voice:
                voice = message.voice
                temp_path = await _download_file_via_bot(
                    info_bot_instance,
                    voice.file_id,
                    filename_hint=f"{voice.file_unique_id}.ogg",
                )
                if temp_path is None:
                    return
                temp_paths.append(temp_path)
                await target_bot.send_voice(
                    chat_id=user_id,
                    voice=FSInputFile(temp_path),
                    caption=caption_text,
                )
            elif message.audio:
                audio = message.audio
                temp_path = await _download_file_via_bot(
                    info_bot_instance,
                    audio.file_id,
                    filename_hint=getattr(audio, "file_name", None),
                )
                if temp_path is None:
                    return
                temp_paths.append(temp_path)
                await target_bot.send_audio(
                    chat_id=user_id,
                    audio=FSInputFile(temp_path),
                    caption=caption_text,
                )
            else:
                await log_info(
                    "[support_admin_reply] неподерживаемый тип вложений",
                    type_msg="warning",
                    user_id=admin_id,
                )
                return
            await log_info(f"Support media/doc delivered to user={user_id} (lang={lang})", type_msg="info")
        except TelegramForbiddenError:
            await log_info(
                f"Не удалось отправить медиа пользователю {user_id}: бот заблокирован или отсутствует диалог.",
                type_msg="error",
            )
            return
        except Exception as e:
            await log_info(f"Ошибка отправки медиа пользователю {user_id}: {e}", type_msg="error")
            return
        finally:
            for temp_path in temp_paths:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        entry = _compose_support_entry(
            author="admin",
            text=(message.caption or "").strip() or None,
            attachments=_extract_message_attachments(message),
            source="admin_chat",
            message=message,
        )
        await _store_support_entry(user_id, entry)

        toast = msgs.get("profile_support_new_reply_toast")
        if toast:
            try:
                await notify_user(user_id, toast, level="info", position="top")
            except Exception as notify_error:
                await log_info(
                    f"support_admin_reply: не удалось показать уведомление пользователю {user_id}: {notify_error}",
                    type_msg="warning",
                )

        await log_info(
            f"[support_admin_reply] медиа ответ сохранён для user_id={user_id}",
            type_msg="info",
            user_id=admin_id,
        )
    except Exception as e:
        await log_info(f"Критическая ошибка обработчика ответа техподдержки: {e}", type_msg="error")


# ----------------------------------------------------------------------------
# 7) Любые НЕ-reply сообщения в служебном чате — игнорируем
# ----------------------------------------------------------------------------

@admin_router.message(
    F.chat.id == SUPPORT_CHAT_ID,
    ~F.reply_to_message,
)
async def support_ignore_plain_group_messages(message: Message):
    """Служебный чат: игнорировать любые сообщения, которые не являются reply на обращения."""
    if SUPPORT_THREAD_ID is not None:
        if getattr(message, "message_thread_id", None) != SUPPORT_THREAD_ID:
            return
    return
