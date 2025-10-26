# handlers/inline_kb_a.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, Any

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.config_from_db import get_all_config


def _fmt_dt(dt):
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(dt) if dt is not None else "—"

async def build_admin_kb() -> InlineKeyboardMarkup:
    cfg: Dict[str, Any] = await get_all_config()
    updated = _fmt_dt(cfg.get("updated_at"))
    chk_country = bool(cfg.get("check_country", False))
    use_region  = bool(cfg.get("region_in_bot", True))
    stars = bool(cfg.get("stars_enabled", False))
    scan = int(cfg.get("recruitment_scan_intervel", 30) or 30)
    maxm = int(cfg.get("recruitment_max_minutes", 15) or 15)

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📦 Выгрузить config", callback_data="admin:export_config")],

        # ── Управление деревом стран/земель
        [
            InlineKeyboardButton(text="➕ Страна", callback_data="admin:add_country"),
            InlineKeyboardButton(text="➖ Страна", callback_data="admin:remove_country"),
        ],
        [
            InlineKeyboardButton(text="➕ Земля", callback_data="admin:add_region"),
            InlineKeyboardButton(text="➖ Земля", callback_data="admin:remove_region"),
        ],

        # ── Города
        [
            InlineKeyboardButton(text="➕ Добавить город", callback_data="admin:add_city"),
            InlineKeyboardButton(text="➖ Убрать город",  callback_data="admin:remove_city"),
        ],

        # ── Тумблеры/настройки
        [InlineKeyboardButton(text=f"🌍 Проверка страны: {'Вкл' if chk_country else 'Выкл'}",
                              callback_data="admin:toggle_check_country")],
        [InlineKeyboardButton(text=f"🗺️ Проверка земли: {'Вкл' if use_region else 'Выкл'}",
                              callback_data="admin:toggle_region_in_bot")],
        [InlineKeyboardButton(text=f"⭐ Stars: {'Вкл' if stars else 'Выкл'}",
                              callback_data="admin:toggle_stars")],
        [InlineKeyboardButton(text=f"⏱ Скан свободных: {scan} сек", callback_data="admin:set_scan")],
        [InlineKeyboardButton(text=f"⏳ Поиск водителя: {maxm} мин", callback_data="admin:set_max")],

        # ── Действия
        [InlineKeyboardButton(text="🚫 Заблокировать пользователя (user_id)", callback_data="admin:block_user")],
        [InlineKeyboardButton(text="🔓 Разблокировать пользователя (user_id)", callback_data="admin:unblock_user")],  # ← НОВОЕ
        [InlineKeyboardButton(text="🗑 Удалить пользователя (user_id)", callback_data="admin:delete_user")],


        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)