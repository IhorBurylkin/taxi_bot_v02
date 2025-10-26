from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def verification_inline_kb() -> InlineKeyboardMarkup:
    """Клавиатура для верификации водителя."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="verify_driver"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data="reject_driver"),
        ],
    ])
    return keyboard