# inline_kb_commands.py
from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config.config import MESSAGES, DEFAULT_LANGUAGES, MAIN_DOMAIN
from config.config_utils import lang_dict


def get_start_inline_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=lang_dict('start_reg_form', lang),
            web_app=WebAppInfo(url=f"{MAIN_DOMAIN}/start_reg_form"),  # <-- ВАЖНО: web_app вместо url
        )
    ]])

def get_verifed_inline_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=lang_dict('open_app', lang),
            web_app=WebAppInfo(url=f"{MAIN_DOMAIN}/main_app?tab=main"),  # <-- ВАЖНО: web_app вместо url
        )
    ]])