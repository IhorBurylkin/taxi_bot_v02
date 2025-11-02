from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.config_utils import lang_dict


def cancel_support_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру с кнопкой отмены для окна техподдержки."""
    text = lang_dict("profile_support_cancel", lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data="cancel_support")]]
    )
