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
from typing import Optional

from aiogram import F, Router, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, CallbackQuery, User
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
from db.db_utils import get_user_data
from keyboards.inline_kb import cancel_support_keyboard

# ----------------------------------------------------------------------------
# 0) Импорты и константы
# ----------------------------------------------------------------------------

router = Router()

SUPPORT_CHAT_ID: int = int(LOGGING_SETTINGS_TO_SEND_SUPPORT.get("chat_id", 0))
SUPPORT_THREAD_ID = LOGGING_SETTINGS_TO_SEND_SUPPORT.get("message_thread_id")  # может быть None

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
        await _send_support_entry(message, user_row, text_for_header=(message.text or "").strip())

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

        lang = await user_lang(message.from_user.id)
        await message.answer(_msgs(lang).get("support_sent") or "✅ Сообщение передано в техподдержку. Спасибо!")

        await state.clear()
        await log_info(f"Support message sent (document) by user={message.from_user.id}", type_msg="info")
    except Exception as e:
        await log_info(f"Ошибка отправки документа в техподдержку: {e}", type_msg="error")


# ----------------------------------------------------------------------------
# 6) Ответ админа из группы → пользователю (по reply)
# ----------------------------------------------------------------------------

@router.message(
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
        if SUPPORT_THREAD_ID is not None:
            if getattr(message, "message_thread_id", None) != SUPPORT_THREAD_ID:
                # Другой топик — игнорируем молча
                return

        # Поднимаемся по reply-цепочке и ищем user_id в шапке
        src = message.reply_to_message
        user_id = None
        depth = 0
        while src and depth < REPLY_CHAIN_MAX_DEPTH and not user_id:
            payload = (src.text or "") or (src.caption or "")
            user_id = _extract_user_id_from_support_stub(payload)
            src = getattr(src, "reply_to_message", None)
            depth += 1

        if not user_id:
            # Нет id — просто игнорируем без шума
            return

        lang = await user_lang(user_id)
        msgs = _msgs(lang)

        # Текст
        if message.text and message.text.strip():
            txt = message.text.strip()
            reply_text_tpl = msgs.get("support_reply_text") or "🛟 Ответ техподдержки:\n\n{text}"
            reply_text = reply_text_tpl.format(text=txt)
            try:
                await message.bot.send_message(chat_id=user_id, text=reply_text)
                await log_info(f"Support reply delivered to user={user_id} (lang={lang})", type_msg="info")
            except TelegramForbiddenError:
                await log_info(
                    f"Не удалось отправить ответ пользователю {user_id}: бот заблокирован пользователем или отсутствует диалог.",
                    type_msg="error",
                )
            except Exception as e:
                await log_info(f"Ошибка отправки текстового ответа пользователю {user_id}: {e}", type_msg="error")
            return

        # Медиа/документ
        try:
            await message.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            await log_info(f"Support media/doc delivered to user={user_id} (lang={lang})", type_msg="info")
        except TelegramForbiddenError:
            await log_info(
                f"Не удалось отправить медиа пользователю {user_id}: бот заблокирован или отсутствует диалог.",
                type_msg="error",
            )
        except Exception as e:
            await log_info(f"Ошибка отправки медиа пользователю {user_id}: {e}", type_msg="error")

    except Exception as e:
        await log_info(f"Критическая ошибка обработчика ответа техподдержки: {e}", type_msg="error")


# ----------------------------------------------------------------------------
# 7) Любые НЕ-reply сообщения в служебном чате — игнорируем
# ----------------------------------------------------------------------------

@router.message(
    (F.chat.type.in_({ChatType.SUPERGROUP, ChatType.GROUP})) &
    (F.chat.id == SUPPORT_CHAT_ID) &
    (F.reply_to_message == None),
)
async def support_ignore_plain_group_messages(message: Message):
    """Служебный чат: игнорировать любые сообщения, которые не являются reply на обращения."""
    if SUPPORT_THREAD_ID is not None:
        if getattr(message, "message_thread_id", None) != SUPPORT_THREAD_ID:
            return
    return
