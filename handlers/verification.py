from __future__ import annotations

import re
from aiogram import Router, F
from aiogram.types import CallbackQuery

from db.db_utils import update_table
from keyboards.inline_kb_commands import get_start_inline_kb, get_verifed_inline_kb

from config.config import SUPPORTED_LANGUAGES
from config.config_utils import lang_dict
from web.web_notify import notify_user
from log.log import log_info

import bot_instance

router = Router()

LABEL_VERIFIED = "ВЕРИФИЦИРОВАН"
LABEL_REJECTED = "НЕ ВЕРИФИЦИРОВАН"


def _pick_lang(cq: CallbackQuery) -> str:
    code = (cq.from_user.language_code or "").lower()[:2]
    return code if code in SUPPORTED_LANGUAGES else "en"


def _extract_user_id(text: str | None) -> int | None:
    """Ожидаем строку вида 'User ID: 123456789' внутри текста сообщения."""
    if not text:
        return None
    m = re.search(r"User ID:\s*(\d+)", text)
    return int(m.group(1)) if m else None


async def _append_status_and_drop_kb(cq: CallbackQuery, label: str) -> None:
    """Удаляем инлайн-клавиатуру и дописываем статус к тексту/подписи сообщения."""
    msg = cq.message
    text = msg.text or msg.caption or ""
    new_text = text if label in text else f"{text}\n\n{label}"
    if msg.photo:
        await msg.edit_caption(caption=new_text, reply_markup=None)
    else:
        await msg.edit_text(new_text, reply_markup=None)


@router.callback_query(F.data == "verify_driver")
async def on_verify_driver(cq: CallbackQuery) -> None:
    lang = _pick_lang(cq)
    await cq.answer()  # ACK
    uid: int | None = None

    try:
        await log_info(f"[verify_driver] клик модератора: admin_id={cq.from_user.id}", type_msg="info")

        uid = _extract_user_id(cq.message.text or cq.message.caption)
        if not uid:
            await log_info("[verify_driver] не найден User ID в тексте/подписи сообщения", type_msg="warning")
            await cq.message.answer("User ID не найден в сообщении")
            return

        await update_table("users", uid, {"verified_driver": True})
        await log_info(f"[verify_driver] БД обновлена: users.verified_driver=True, uid={uid}", type_msg="info")

        driver_bot = bot_instance.bot or cq.bot
        if not driver_bot:
            await log_info("[verify_driver] основной бот не инициализирован (driver_bot is None)", type_msg="error")
            await cq.message.answer("Ошибка: основной бот ещё не инициализирован.")
            return

        await driver_bot.send_message(
            chat_id=uid, 
            text=lang_dict("verified", lang),
            reply_markup=get_verifed_inline_kb(lang),
        )
        await log_info(f"[verify_driver] ЛС отправлено пользователю: uid={uid}", type_msg="info")

        notified = await notify_user(uid, lang_dict("verified", lang), level="positive", position="center")
        await log_info(f"[verify_driver] notify_user result={bool(notified)} uid={uid}", type_msg="info")

        await _append_status_and_drop_kb(cq, LABEL_VERIFIED)
        await log_info(f"[verify_driver] исходное сообщение помечено '{LABEL_VERIFIED}', uid={uid}", type_msg="info")

    except Exception as e:
        await log_info(f"[verify_driver][ОШИБКА] uid={uid} | {e!r}", type_msg="error")
        try:
            await cq.message.answer("Произошла ошибка при верификации. Попробуйте ещё раз.")
        except Exception:
            pass
        raise


@router.callback_query(F.data == "reject_driver")
async def on_reject_driver(cq: CallbackQuery) -> None:
    lang = _pick_lang(cq)
    await cq.answer()  # ACK
    uid: int | None = None

    try:
        await log_info(f"[reject_driver] клик модератора: admin_id={cq.from_user.id}", type_msg="info")

        uid = _extract_user_id(cq.message.text or cq.message.caption)
        if not uid:
            await log_info("[reject_driver] не найден User ID в тексте/подписи сообщения", type_msg="warning")
            await cq.message.answer("User ID не найден в сообщении")
            return

        driver_bot = bot_instance.bot or cq.bot
        if not driver_bot:
            await log_info("[reject_driver] основной бот не инициализирован (driver_bot is None)", type_msg="error")
            await cq.message.answer("Ошибка: основной бот ещё не инициализирован.")
            return

        await driver_bot.send_message(
            chat_id=uid,
            text=lang_dict("rejected", lang),
            reply_markup=get_start_inline_kb(lang),
        )
        await log_info(f"[reject_driver] ЛС отправлено пользователю: uid={uid}", type_msg="info")

        notified = await notify_user(uid, lang_dict("rejected", lang), level="warning", position="center")
        await log_info(f"[reject_driver] notify_user result={bool(notified)} uid={uid}", type_msg="info")

        await _append_status_and_drop_kb(cq, LABEL_REJECTED)
        await log_info(f"[reject_driver] исходное сообщение помечено '{LABEL_REJECTED}', uid={uid}", type_msg="info")

    except Exception as e:
        await log_info(f"[reject_driver][ОШИБКА] uid={uid} | {e!r}", type_msg="error")
        try:
            await cq.message.answer("Произошла ошибка при отклонении. Попробуйте ещё раз.")
        except Exception:
            pass
        raise
