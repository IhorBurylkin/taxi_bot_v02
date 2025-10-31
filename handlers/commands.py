from aiogram import types, Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import Message, ReplyKeyboardRemove
from db.db_utils import user_exists, insert_into_table, get_user_data
from log.log import log_info, send_info_msg
from keyboards.inline_kb_commands import get_start_inline_kb
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES, USERS_TABLE, MESSAGES

router = Router()

router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

async def _is_user_blocked(user_id: int) -> bool:
    """
    Унифицированная проверка блокировки пользователя.
    Поддерживает разные схемы: is_blocked/blocked/black_list/status == 'blocked'.
    """
    try:
        row = await get_user_data(USERS_TABLE, user_id)
        if not row:
            return False
        v = (
            row.get("is_blocked")
            or row.get("blocked")
            or row.get("black_list")
            or (row.get("status") == "blocked")
        )
        return bool(v)
    except Exception as e:
        # если не смогли прочитать — не блокируем по ошибке, но логируем
        await log_info(
            f"_is_user_blocked failed for {user_id}: {e}",
            type_msg="warning",
            user_id=user_id,
        )
        return False

@router.message(Command("start"))
async def send_welcome(message: types.Message):
    try:
        user_id: int | None = message.from_user.id
        await log_info(
            f"Получена команда /start от пользователя {user_id}",
            type_msg="info",
            user_id=user_id,
        )
        chat_id = message.chat.id if message.chat.type == ChatType.PRIVATE else message.from_user.id
        user_lang = message.from_user.language_code
        lang = user_lang if user_lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGES

        user_id_exists = await user_exists(user_id)
        await log_info(
            f"Проверка существования пользователя {user_id} в БД: {'найден' if user_id_exists else 'не найден'}",
            type_msg="info",
            user_id=user_id,
        )

        if user_id_exists == False:
            user_data = {
                "user_id": user_id,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "language": lang,
                "role": "unknown"
            }
            await insert_into_table(USERS_TABLE, user_data)
            await send_info_msg(text=f'Тип сообщения: Инфо\nНовый пользователь!\nUsername: {user_data["username"]}\nFirst name: {user_data["first_name"]}\nUser ID: {user_data["user_id"]}', type_msg_tg="new_users")
            await log_info(
                f'Новый пользователь Username: {user_data["username"]} '
                f'First name: {user_data["first_name"]} '
                f'User ID: {user_data["user_id"]}',
                type_msg="info",
                user_id=user_id,
            )

        if await _is_user_blocked(user_id):
            # локализация, если есть ключ; иначе — дефолтный текст
            text = (
                (MESSAGES.get(lang, {}) or {}).get("blocked_user_info")
                or "Ваш доступ к сервису временно заблокирован.\n"
                   "Если Вы считаете это ошибкой — напишите в поддержку командой /support."
            )
            await log_info(
                f"/start: user {user_id} is blocked → show blocked notice",
                type_msg="info",
                user_id=user_id,
            )
            await message.answer(text, reply_markup=ReplyKeyboardRemove())
            return

        await message.answer(
            MESSAGES[lang]["start_greeting"],
            reply_markup=get_start_inline_kb(lang),
        )

    except Exception as e:
        await log_info(
            f"Ошибка в send_welcome: {e}",
            type_msg="error",
            user_id=user_id,
        )
        raise