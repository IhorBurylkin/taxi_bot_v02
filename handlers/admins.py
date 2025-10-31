from __future__ import annotations
import json
import asyncio
from typing import Any, Dict, List
from datetime import datetime, timezone
import bot_instance

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.config import LOGGING_SETTINGS_TO_SEND_ADMINS as ADMINS_CFG
from config.config_from_db import (
    get_cities, get_all_config, ensure_config_exists,
    _upsert_country_sql, _remove_country_sql,
    _upsert_region_sql, _remove_region_sql,
    list_countries, list_regions, list_cities,
    _upsert_city_sql, _remove_city_sql_tree,
)
from db.db_table_init import get_connection, release_connection
from log.log import log_info

from keyboards.inline_kb_a import build_admin_kb
from keyboards.reply_kb import reply_keyboard

CONFIG_TABLE = "config"
CONFIG_ID = 1  # единственная запись
INACTIVITY_TIMEOUT = 300

router = Router(name="admins")


class AdminStates(StatesGroup):
    add_city = State()
    remove_city = State()
    set_scan = State()
    set_max = State()
    add_country = State()
    remove_country = State()
    add_region_country = State()
    add_region_region = State()
    remove_region_country = State()
    remove_region_region = State()
    add_city_country = State()
    add_city_region = State()
    remove_city_country = State()
    remove_city_region = State()
    delete_user_id = State()
    confirm_delete_user = State()
    block_user_id = State()
    confirm_block_user = State()
    unblock_user_id = State()         
    confirm_unblock_user = State() 


# --- Унифицированное логирование ---

async def _log_admin(message: str, *, type_msg: str, actor_id: int | None = None) -> None:
    await log_info(message, type_msg=type_msg, log="admins", user_id=actor_id)


# ====== Утилиты доступа/проверки контекста ======

def _allowed_place(obj: Any) -> bool:
    """
    Команда/кнопки принимаются ТОЛЬКО info_bot'ом и только в указанном
    служебном чате/теме из LOGGING_SETTINGS_TO_SEND_SUPPORT.
    """
    if not ADMINS_CFG or not ADMINS_CFG.get("permission"):
        return False

    need_chat = int(ADMINS_CFG.get("chat_id"))
    need_thread = ADMINS_CFG.get("message_thread_id")

    chat_ok = getattr(obj.chat, "id", None) == need_chat
    if not chat_ok:
        return False

    # если задан thread id — тоже проверяем
    if need_thread is not None:
        return getattr(obj, "message_thread_id", None) == need_thread
    return True

async def _delete_user_sql(user_id: int) -> bool:
    conn = await get_connection()
    try:
        row = await conn.fetchrow("DELETE FROM users WHERE user_id = $1 RETURNING user_id", user_id)
        return bool(row)
    except Exception as e:
        await _log_admin(f"delete user failed: {e}", type_msg="error", actor_id=user_id)
        return False
    finally:
        await release_connection(conn)

def _export_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:export_config:back")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:export_config:cancel")],
    ])

async def _save_prompt(state: FSMContext, msg: Message) -> None:
    await state.update_data(prompt_msg_id=msg.message_id, prompt_chat_id=msg.chat.id)

async def _delete_saved_prompt(state: FSMContext) -> None:
    data = await state.get_data()
    mid = data.get("prompt_msg_id")
    cid = data.get("prompt_chat_id")
    if mid and cid:
        try:
            ib = getattr(bot_instance, "info_bot", None)
            if ib:
                await ib.delete_message(cid, mid)
        except Exception:
            pass
        # очистить, чтобы не дергать второй раз
        await state.update_data(prompt_msg_id=None, prompt_chat_id=None)

async def _edit_saved_prompt_text(state: FSMContext, text: str):
    data = await state.get_data()
    mid = data.get("prompt_msg_id")
    cid = data.get("prompt_chat_id")
    ib = getattr(bot_instance, "info_bot", None)
    if ib and mid and cid:
        try:
            await ib.edit_message_text(chat_id=cid, message_id=mid, text=text, reply_markup=_cancel_kb())
        except Exception:
            pass

def _fmt_user_card(d: Dict[str, Any]) -> str:
    if not d: return "—"
    lines = []
    for k, v in d.items():
        lines.append(f"{k}: {v if v is not None else '—'}")
    return "\n".join(lines)

async def _send_panel(msg: Message, state: FSMContext) -> Message:
    kb = await build_admin_kb()
    panel = await msg.answer("Админ-панель", reply_markup=kb, disable_notification=True)
    await _start_panel_timer(panel, state)
    return panel

async def _block_user_sql(user_id: int) -> bool:
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            "UPDATE users SET black_list = TRUE WHERE user_id = $1 RETURNING user_id",
            user_id
        )
        return bool(row)
    except Exception as e:
        await _log_admin(f"block user failed: {e}", type_msg="error", actor_id=user_id)
        return False
    finally:
        await release_connection(conn)

async def _unblock_user_sql(user_id: int) -> bool:
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            "UPDATE users SET black_list = FALSE WHERE user_id = $1 RETURNING user_id",
            user_id
        )
        return bool(row)
    except Exception as e:
        await _log_admin(f"unblock user failed: {e}", type_msg="error", actor_id=user_id)
        return False
    finally:
        await release_connection(conn)

async def _remove_panel_markup_safely(message) -> None:
    """
    Убирает инлайн-клавиатуру у сообщения.
    Если уже убрано или редактирование запрещено — молча игнорируем,
    иначе пробуем отправить отдельное сообщение «Панель закрыта».
    """
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        msg = (getattr(e, "message", None) or str(e)).lower()
        if "message is not modified" in msg or "message to edit not found" in msg:
            return
        try:
            await message.answer("Панель закрыта", disable_notification=True)
        except Exception:
            pass

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")]]
    )

def _make_country_kb(prefix: str, items: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in items:
        rows.append([InlineKeyboardButton(text=name, callback_data=f"admin:{prefix}:country:{name}")])
    # ↓↓↓ добавили «Назад» в главное меню
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:{prefix}:back")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _edit_panel_main(cb_or_msg, state: FSMContext, subtitle: str, kb: InlineKeyboardMarkup | None):
    msg = cb_or_msg.message if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg
    try:
        await msg.edit_text(subtitle, reply_markup=kb)
    except TelegramBadRequest as e:
        m = (getattr(e, "message", "") or str(e)).lower()
        if "message is not modified" in m:
            try:
                await msg.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass
        else:
            # ⇣⇣⇣ вместо msg.answer(...) создаём/редактируем панель строго по сохранённым chat/thread
            await _edit_panel_main_by_state(state, subtitle, kb)  # (создание в нужном чате происходит внутри)


async def _edit_panel_main_by_state(state: FSMContext, subtitle: str, kb: InlineKeyboardMarkup | None):
    """
    Редактирует ГЛАВНОЕ МЕНЮ (без «дерева») по сохранённым panel_chat_id/panel_msg_id/(опц.)panel_thread_id.
    Если панель не найдена — создаёт новую в том же чате и том же топике.
    """
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    panel_chat_id = data.get("panel_chat_id")
    panel_thread_id = data.get("panel_thread_id")

    # Фоллбэки из конфига (если первый запуск и ещё ничего не сохранено)
    chat_id = int(panel_chat_id or ADMINS_CFG.get("chat_id"))
    thread_id = panel_thread_id if panel_thread_id is not None else ADMINS_CFG.get("message_thread_id")

    ib = getattr(bot_instance, "info_bot", None)
    if not ib:
        return

    if panel_msg_id:
        try:
            # edit в том же чате (thread_id не нужен для edit)
            await ib.edit_message_text(chat_id=chat_id, message_id=panel_msg_id, text=subtitle, reply_markup=kb)
            return
        except Exception:
            # если не удалось — будем создавать заново ниже
            pass

    # создаём новую панель ИМЕННО в том же чате и, если есть, в том же топике
    kwargs = {"chat_id": chat_id, "text": subtitle, "reply_markup": kb}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    panel = await ib.send_message(**kwargs, disable_notification=True)
    await _start_panel_timer(panel, state)

async def _turn_into_panel(message: Message, state: FSMContext, subtitle: str = "Админ-панель"):
    """
    Редактирует ПРЯМО ЭТО сообщение (например, с «Подтвердить…») в админ-панель.
    Ставит таймер и сохраняет msg_id/chat_id/thread_id как «живую» панель.
    """
    kb = await build_admin_kb()
    try:
        await message.edit_text(subtitle, reply_markup=kb)
    except TelegramBadRequest:
        # если не редактируется — попробуем убрать клаву
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    await _start_panel_timer(message, state) 

def _make_region_kb(prefix: str, country: str, items: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in items:
        rows.append([InlineKeyboardButton(
            text=name,
            callback_data=f"admin:{prefix}:region:{country}|{name}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:{prefix}:back:{country}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _make_city_kb(prefix: str, country: str, region: str, items: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for name in items:
        rows.append([InlineKeyboardButton(
            text=name,
            callback_data=f"admin:{prefix}:city:{country}|{region}|{name}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:{prefix}:back:{country}|{region}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _delete_panel_safely(message) -> None:
    """Удалить сообщение с панелью; если нельзя — убрать inline-клавиатуру."""
    try:
        await message.delete()
    except TelegramBadRequest:
        await _remove_panel_markup_safely(message)
    except Exception:
        await _remove_panel_markup_safely(message)

async def _set_state_timer(message, state: FSMContext, expected_state: State):
    """Ставит таймер: если через 5 минут всё ещё expected_state — удаляет message и сбрасывает state."""
    # отменим прежний таймер этого типа, если был
    await _cancel_timer(state, "state_timer")

    async def _job():
        try:
            await asyncio.sleep(INACTIVITY_TIMEOUT)
            cur = await state.get_state()
            if cur == expected_state.state:
                # пробуем удалить сообщение с кнопкой «Отмена»
                try:
                    await message.delete()
                except Exception:
                    pass
                # сбрасываем состояние
                await state.clear()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_job())
    await state.update_data(state_timer=task, state_timer_msg_id=message.message_id)

async def _cancel_state_timer(state: FSMContext):
    await _cancel_timer(state, "state_timer")

async def _start_panel_timer(message, state: FSMContext):
    """Ставит таймер: если 5 минут нет действий, удаляет панель /admin.
    Параллельно сохраняем chat_id и message_thread_id панели, чтобы потом редактировать/создавать её в том же месте.
    """
    await _cancel_timer(state, "panel_timer")

    async def _job():
        try:
            await asyncio.sleep(INACTIVITY_TIMEOUT)
            try:
                await message.delete()
            except Exception:
                # если удалить нельзя — уберём клавиатуру
                try:
                    await message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_job())

    # аккуратно обновляем state: если нет chat/thread у message (например «пустышка» после edit),
    # НЕ затираем прежние panel_chat_id/panel_thread_id
    data = await state.get_data()
    new_data = {
        "panel_timer": task,
        "panel_msg_id": getattr(message, "message_id", data.get("panel_msg_id")),
        "panel_chat_id": getattr(getattr(message, "chat", None), "id", data.get("panel_chat_id")),
        "panel_thread_id": getattr(message, "message_thread_id", data.get("panel_thread_id")),
    }
    await state.update_data(**new_data)

async def _restart_panel_timer(message, state: FSMContext):
    await _start_panel_timer(message, state)

async def _cancel_panel_timer(state: FSMContext):
    await _cancel_timer(state, "panel_timer")

async def _cancel_timer(state: FSMContext, key: str):
    data = await state.get_data()
    task = data.get(key)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    if task:
        # очистим, чтобы не висело в сторе
        data.pop(key, None)
        await state.update_data(**{key: None})

def _fmt_utc(dt) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(dt) if dt is not None else "—"

async def _preview_cities_text() -> str:
    cfg = await get_all_config()
    data = cfg.get("cities") or {}
    updated = _fmt_utc(cfg.get("updated_at"))

    if not isinstance(data, dict) or not data:
        return f"Дерево городов: —\nОбновлено: {updated}"

    lines: List[str] = []
    for country, regions in sorted(data.items()):
        lines.append(f"• {country}")
        if isinstance(regions, dict) and regions:
            for region, cities in sorted(regions.items()):
                if isinstance(cities, list):
                    lines.append(f"   ├─ {region}: {len(cities)}")
                else:
                    lines.append(f"   ├─ {region}: ?")
        else:
            lines.append("   └─ (нет земель)")

    body = "\n".join(lines)
    return f"Дерево городов:\n{body}\nОбновлено: {updated}"

def _bool_ru(v: bool) -> str:
    return "Вкл" if bool(v) else "Выкл"

def _format_cities_tree_readable(cities: Dict[str, Any]) -> str:
    """
    Человекочитаемое дерево:
    ├─ Germany
    │  ├─ Hamburg
    │  │  ├─ Hamburg
    │  └─ Schleswig-Holstein
    │     └─ Flensburg
    └─ ...
    Без ограничений по числу городов.
    """
    if not isinstance(cities, dict) or not cities:
        return "—"

    def branch(is_last: bool) -> tuple[str, str]:
        """Возвращает (вертикальная черта на следующем уровне, символ ветки на этом уровне)."""
        return ("   " if is_last else "│  ", "└─" if is_last else "├─")

    lines: list[str] = []
    countries = sorted(cities.keys())
    for ci, country in enumerate(countries):
        is_last_country = (ci == len(countries) - 1)
        vpad_country, twig_country = branch(is_last_country)
        lines.append(f"{twig_country} {country}")

        regions = cities.get(country, {})
        if not isinstance(regions, dict) or not regions:
            lines.append(f"{vpad_country}└─ (нет земель)")
            continue

        region_names = sorted(regions.keys())
        for ri, region in enumerate(region_names):
            is_last_region = (ri == len(region_names) - 1)
            vpad_region, twig_region = branch(is_last_region)
            # вертикаль страны + ветка земли
            lines.append(f"{vpad_country}{twig_region} {region}")

            city_list = regions.get(region, [])
            if not isinstance(city_list, list) or not city_list:
                lines.append(f"{vpad_country}{vpad_region}└─ —")
                continue

            for cj, city in enumerate(city_list):
                is_last_city = (cj == len(city_list) - 1)
                _vpad_city, twig_city = branch(is_last_city)
                lines.append(f"{vpad_country}{vpad_region}{twig_city} {city}")

    return "\n".join(lines)

def _human_readable_config(cfg: Dict[str, Any]) -> str:
    """
    Собирает понятный для админа отчёт по config.
    """
    updated = _fmt_utc(cfg.get("updated_at"))
    cities = cfg.get("cities") or {}

    parts = [
        "📦 Текущие настройки config",
        "",
        f"🌍 Проверка страны: {_bool_ru(cfg.get('check_country', False))}",
        f"🗺️ Проверка земли:  {_bool_ru(cfg.get('region_in_bot', True))}",
        f"⭐ Stars:            {_bool_ru(cfg.get('stars_enabled', False))}",
        "",
        "⏱ Интервалы",
        f" • Скан свободных: {int(cfg.get('recruitment_scan_intervel', 30) or 30)} сек",
        f" • Поиск водителя: {int(cfg.get('recruitment_max_minutes', 15) or 15)} мин",
        "",
        "🏙️ Дерево городов",
        _format_cities_tree_readable(cities),
        "",
        f"Обновлено: {updated}",
    ]
    text = "\n".join(parts)
    return text
    

# ── «живой» пост: формирование текста и единое редактирование ────────────────
async def _panel_text_with_tree(action_hint: str = "") -> str:
    """
    Возвращает: [Дерево городов]\n\n[action_hint]
    Дерево берём всегда актуальное из БД.
    """
    tree = await _preview_cities_text()
    suffix = f"\n\n{action_hint}" if action_hint else ""
    return f"{tree}{suffix}"

async def _edit_panel(cb_or_msg, state: FSMContext, action_hint: str, kb: InlineKeyboardMarkup | None):
    """
    Редактирует текст (дерево + подсказка) и клавиатуру у «живой» панели.
    - Для CallbackQuery редактируем ТЕКУЩЕЕ сообщение панели.
    - Для Message НЕ редактируем сообщение пользователя, а редактируем/создаём панель по сохранённому panel_msg_id.
    """
    text = await _panel_text_with_tree(action_hint)

    # Ветка CallbackQuery: можем безопасно редактировать cb.message (это и есть панель).
    if isinstance(cb_or_msg, CallbackQuery):
        msg = cb_or_msg.message
        await _restart_panel_timer(msg, state)
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            m = (getattr(e, "message", "") or str(e)).lower()
            if "message is not modified" in m:
                try:
                    await msg.edit_reply_markup(reply_markup=kb)
                    return
                except Exception:
                    pass
            # если не получилось — ниже пойдем через state

    # Ветка Message (или фолбэк): никогда не редактируем сообщение пользователя,
    # а правим панель строго по сохранённым chat/thread/msg_id.
    await _edit_panel_main_by_state(state, text, kb)

async def _clear_inline_kb(cb_or_msg):
    """Снять клавиатуру у текущего «живого» поста (если нужно без изменения текста)."""
    msg = cb_or_msg.message if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ====== Низкоуровневые апдейтеры config ======

async def _toggle_stars_enabled() -> Dict[str, Any]:
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET stars_enabled = NOT stars_enabled,
                   updated_at = now()
             WHERE id = $1
         RETURNING stars_enabled, updated_at
            """,
            CONFIG_ID,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)

async def _toggle_check_country() -> Dict[str, Any]:
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET check_country = NOT COALESCE(check_country, FALSE),
                   updated_at = now()
             WHERE id = $1
         RETURNING check_country, updated_at
            """, CONFIG_ID)
        return dict(row or {})
    finally:
        await release_connection(conn)

async def _toggle_region_in_bot() -> Dict[str, Any]:
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET region_in_bot = NOT COALESCE(region_in_bot, TRUE),
                   updated_at = now()
             WHERE id = $1
         RETURNING region_in_bot, updated_at
            """, CONFIG_ID)
        return dict(row or {})
    finally:
        await release_connection(conn)

async def _set_scan_intervel(seconds: int) -> Dict[str, Any]:
    if seconds <= 0:
        raise ValueError("seconds must be > 0")
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET recruitment_scan_intervel = $1,
                   updated_at = now()
             WHERE id = $2
         RETURNING recruitment_scan_intervel, updated_at
            """,
            seconds, CONFIG_ID,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)

async def _set_max_minutes(minutes: int) -> Dict[str, Any]:
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET recruitment_max_minutes = $1,
                   updated_at = now()
             WHERE id = $2
         RETURNING recruitment_max_minutes, updated_at
            """,
            minutes, CONFIG_ID,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)

async def _clear_state_preserve_panel(state: FSMContext):
    data = await state.get_data()
    keep = {k: data.get(k) for k in ("panel_msg_id","panel_chat_id","panel_thread_id","panel_timer") if data.get(k) is not None}
    await state.clear()
    if keep:
        await state.update_data(**keep)

# ====== Хэндлеры ======

@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if not _allowed_place(msg):
        return
    await state.clear()
    kb = await build_admin_kb()
    panel = await msg.answer("Админ-панель", reply_markup=kb, disable_notification=True)
    await _start_panel_timer(panel, state)


@router.callback_query(F.data == "admin:noop")
async def cb_noop(cb: CallbackQuery):
    if not _allowed_place(cb.message):
        return
    await cb.answer(" ")


@router.callback_query(F.data == "admin:refresh")
async def cb_refresh(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await cb.answer("Обновлено")
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, "Админ-панель", kb)


@router.callback_query(F.data == "admin:toggle_stars")
async def cb_toggle_stars(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    admin_id = cb.from_user.id
    upd = await _toggle_stars_enabled()
    await cb.answer(f"Stars: {'Включено' if upd.get('stars_enabled') else 'Выключено'}")
    await _log_admin(
        f"[admin {admin_id}] toggle stars_enabled → {bool(upd.get('stars_enabled'))}",
        type_msg="info",
        actor_id=admin_id,
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, "Админ-панель", kb)


@router.callback_query(F.data == "admin:set_scan")
async def cb_set_scan(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await state.set_state(AdminStates.set_scan)
    await _cancel_panel_timer(state)
    await _delete_panel_safely(cb.message)
    prompt = await cb.message.answer("Введите интервал сканирования свободных водителей в секундах (> 0).", reply_markup=_cancel_kb(), disable_notification=True)
    await _save_prompt(state, prompt)
    await _set_state_timer(prompt, state, AdminStates.set_scan)


@router.message(AdminStates.set_scan)
async def st_set_scan(msg: Message, state: FSMContext):
    if not _allowed_place(msg):
        return
    try:
        seconds = int((msg.text or "").strip())
        if seconds <= 0:
            raise ValueError
    except Exception:
        await _edit_saved_prompt_text(state, "❗ Нужно целое число > 0. Попробуйте ещё раз.")
        try: await msg.delete()
        except: pass
        return

    upd = await _set_scan_intervel(seconds)
    await log_info(
        f"[admin {msg.from_user.id}] set recruitment_scan_intervel → {upd.get('recruitment_scan_intervel')}",
        type_msg="info", log="admins"
    )

    await _delete_saved_prompt(state)
    try:
        await msg.delete()
    except:
        pass
    # 2) очистить состояние, сохранив привязку к «живому» посту панели
    await _clear_state_preserve_panel(state)
    # 3) обновить/создать панель
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Установлено: {seconds} сек.", kb)


@router.callback_query(F.data == "admin:set_max")
async def cb_set_max(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await state.set_state(AdminStates.set_max)
    await _cancel_panel_timer(state)
    await _delete_panel_safely(cb.message)
    prompt = await cb.message.answer("Введите время поиска водителя в минутах (> 0).", reply_markup=_cancel_kb(), disable_notification=True)
    await _save_prompt(state, prompt)
    await _set_state_timer(prompt, state, AdminStates.set_max)


@router.message(AdminStates.set_max)
async def st_set_max(msg: Message, state: FSMContext):
    if not _allowed_place(msg):
        return
    try:
        minutes = int((msg.text or "").strip())
        if minutes <= 0:
            raise ValueError
    except Exception:
        await _edit_saved_prompt_text(state, "❗ Нужно целое число > 0. Попробуйте ещё раз.")
        try: await msg.delete()
        except: pass
        return

    upd = await _set_max_minutes(minutes)
    await log_info(
        f"[admin {msg.from_user.id}] set recruitment_max_minutes → {upd.get('recruitment_max_minutes')}",
        type_msg="info", log="admins"
    )
    await _delete_saved_prompt(state)
    try:
        await msg.delete()
    except:
        pass
    # 2) сохранить привязку к панели и очистить остальное
    await _clear_state_preserve_panel(state)
    # 3) перерисовать панель
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Установлено: {minutes} сек.", kb)


@router.callback_query(F.data == "admin:close")
async def cb_close(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await state.clear()
    await _cancel_panel_timer(state)
    try:
        await cb.message.delete()
    except Exception:
        await _clear_inline_kb(cb)
    await cb.answer("Панель закрыта")

@router.callback_query(F.data == "admin:cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await _cancel_state_timer(state)
    await state.clear()
    kb = await build_admin_kb()
    await _edit_panel_main(cb, state, "Админ-панель", kb)  # ← без дерева
    await cb.answer("Отменено")

@router.callback_query(F.data == "admin:add_country")
async def cb_add_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.set_state(AdminStates.add_country)
    await _cancel_panel_timer(state)
    await _edit_panel(cb, state, "Введите НАЗВАНИЕ СТРАНЫ для добавления.", _cancel_kb())
    await cb.answer()

@router.message(AdminStates.add_country)
async def st_add_country(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    country = (msg.text or "").strip()
    if not country:
        await _edit_panel(msg, state, "Пусто. Введите название страны ещё раз.", _cancel_kb())
        return

    await _upsert_country_sql(country)
    await log_info(
        f"[admin {msg.from_user.id}] cities: add country {country}",
        type_msg="info", log="admins"
    )
    await ensure_config_exists()
    try:
        cfg = await get_all_config()
        await log_info(
            f"[admin {msg.from_user.id}] after add country: cities={json.dumps(cfg.get('cities'), ensure_ascii=False)}",
            type_msg="info", log="admins"
        )
    except Exception:
        pass

    await _cancel_state_timer(state)
    await _clear_state_preserve_panel(state)
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Страна добавлена/обновлена: {country}", kb)

    # (опционально) подчистим сам пользовательский ввод
    try: await msg.delete()
    except: pass


@router.message(AdminStates.remove_region_country)
async def st_remove_region_country(msg: Message, state: FSMContext):
    if not _allowed_place(msg):
        return
    country = (msg.text or "").strip()
    if not country:
        await _edit_panel(msg, state, "Пусто. Введите страну ещё раз.", _cancel_kb())
        return
    await state.update_data(region_country=country)
    await state.set_state(AdminStates.remove_region_region)
    await _edit_panel(msg, state, f"Страна: {country}\nВведите НАЗВАНИЕ ЗЕМЛИ для удаления (каскадно).", _cancel_kb())
    # удаляем пользовательское сообщение с вводом
    try: await msg.delete()
    except: pass

@router.message(AdminStates.remove_region_region)
async def st_remove_region_region(msg: Message, state: FSMContext):
    if not _allowed_place(msg):
        return
    data = await state.get_data()
    country = (data.get("region_country") or "").strip()
    region = (msg.text or "").strip()
    if not (country and region):
        await _edit_panel(msg, state, "Пусто. Введите данные ещё раз.", _cancel_kb())
        return
    await _remove_region_sql(country, region)
    await log_info(
        f"[admin {msg.from_user.id}] cities: remove region {country} → {region}",
        type_msg="info", log="admins"
    )
    await _cancel_state_timer(state)
    await _clear_state_preserve_panel(state)
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Удалена земля (каскад): {country} → {region}", kb)
    # удаляем пользовательское сообщение с вводом
    try: await msg.delete()
    except: pass

@router.callback_query(F.data == "admin:add_region")
async def cb_add_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.clear()
    await _cancel_panel_timer(state)
    await ensure_config_exists()
    countries = await list_countries()
    if not countries:
        await _edit_panel(cb, state, "В базе нет стран. Сначала добавьте страну.", _cancel_kb())
        await cb.answer(); return
    kb = _make_country_kb("pick_add_region", countries)
    await state.set_state(AdminStates.add_region_country)
    await _edit_panel(cb, state, "Выберите СТРАНУ:", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:pick_add_region:country:"))
async def cb_pick_add_region_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    country = cb.data.split("admin:pick_add_region:country:", 1)[1]
    await state.update_data(region_country=country)
    await state.set_state(AdminStates.add_region_region)
    await _edit_panel(cb, state, f"Страна: {country}\nВведите НАЗВАНИЕ ЗЕМЛИ:", _cancel_kb())
    await cb.answer()


@router.message(AdminStates.add_region_region)
async def st_add_region_region(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    data = await state.get_data()
    country = (data.get("region_country") or "").strip()
    region = (msg.text or "").strip()
    if not (country and region):
        await _edit_panel(msg, state, "Пусто. Попробуйте ещё раз.", _cancel_kb()); return
    await _upsert_region_sql(country, region)
    await log_info(
        f"[admin {msg.from_user.id}] cities: add region {country} → {region}",
        type_msg="info", log="admins"
    )
    await _cancel_state_timer(state)
    await _clear_state_preserve_panel(state)
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Добавлена земля: {country} → {region}", kb)
    try: await msg.delete()
    except: pass

@router.callback_query(F.data == "admin:add_city")
async def cb_add_city(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.clear(); await _cancel_panel_timer(state); await ensure_config_exists()
    countries = await list_countries()
    if not countries:
        await _edit_panel(cb, state, "В базе нет стран. Сначала добавьте страну.", _cancel_kb())
        await cb.answer(); return
    kb = _make_country_kb("pick_add_city", countries)
    await state.set_state(AdminStates.add_city_country)
    await _edit_panel(cb, state, "Выберите СТРАНУ:", kb)
    await cb.answer()

@router.callback_query(F.data.startswith("admin:pick_add_city:country:"))
async def cb_pick_add_city_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    country = cb.data.split("admin:pick_add_city:country:", 1)[1]
    regions = await list_regions(country)
    if not regions:
        await _edit_panel(cb, state, f"В стране {country} нет земель. Сначала добавьте землю.", _cancel_kb())
        await cb.answer(); return
    await state.update_data(add_city_country=country)
    kb = _make_region_kb("pick_add_city", country, regions)
    await state.set_state(AdminStates.add_city_region)
    await _edit_panel(cb, state, f"Страна: {country}\nВыберите ЗЕМЛЮ:", kb)
    await cb.answer()

@router.callback_query(F.data.startswith("admin:pick_add_city:region:"))
async def cb_pick_add_city_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    payload = cb.data.split("admin:pick_add_city:region:", 1)[1]
    country, region = payload.split("|", 1)
    await state.update_data(add_city_country=country, add_city_region=region)
    await _edit_panel(cb, state, f"{country} → {region}\nВведите НАЗВАНИЕ ГОРОДА:", _cancel_kb())
    await cb.answer()

@router.message(AdminStates.add_city_region)
async def st_add_city_region(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    data = await state.get_data()
    country = (data.get("add_city_country") or "").strip()
    region  = (data.get("add_city_region")  or "").strip()
    city    = (msg.text or "").strip()
    if not (country and region and city):
        await _edit_panel(msg, state, "Пусто. Попробуйте ещё раз.", _cancel_kb()); return
    await _upsert_city_sql(country, region, city)
    await log_info(
        f"[admin {msg.from_user.id}] cities: add city {country} → {region} → {city}",
        type_msg="info", log="admins"
    )
    await _cancel_state_timer(state)
    await _clear_state_preserve_panel(state)
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Добавлен город: {country} → {region} → {city}", kb)
    try: await msg.delete()
    except: pass

@router.callback_query(F.data == "admin:remove_country")
async def cb_remove_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.clear(); await _cancel_panel_timer(state); await ensure_config_exists()
    countries = await list_countries()
    if not countries:
        await _edit_panel(cb, state, "Стран нет.", _cancel_kb()); await cb.answer(); return
    kb = _make_country_kb("del_country", countries)
    await _edit_panel(cb, state, "Удалить СТРАНУ:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_country:country:"))
async def cb_del_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    country = cb.data.split("admin:del_country:country:", 1)[1]
    await _remove_country_sql(country)
    await log_info(
        f"[admin {cb.from_user.id}] cities: remove country {country}",
        type_msg="info", log="admins"
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Удалена страна (каскад): {country}", kb)
    await cb.answer()

@router.callback_query(F.data == "admin:remove_region")
async def cb_remove_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.clear(); await _cancel_panel_timer(state); await ensure_config_exists()
    countries = await list_countries()
    if not countries:
        await _edit_panel(cb, state, "Стран нет.", _cancel_kb()); await cb.answer(); return
    kb = _make_country_kb("del_region", countries)
    await _edit_panel(cb, state, "Выберите СТРАНУ:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_region:country:"))
async def cb_del_region_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    country = cb.data.split("admin:del_region:country:", 1)[1]
    regions = await list_regions(country)
    if not regions:
        await _edit_panel(cb, state, f"В стране {country} нет земель.", _cancel_kb()); await cb.answer(); return
    kb = _make_region_kb("del_region", country, regions)
    await _edit_panel(cb, state, f"Страна: {country}\nУдалить ЗЕМЛЮ:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_region:region:"))
async def cb_del_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    payload = cb.data.split("admin:del_region:region:", 1)[1]
    country, region = payload.split("|", 1)
    await _remove_region_sql(country, region)
    await log_info(
        f"[admin {cb.from_user.id}] cities: remove region {country} → {region}",
        type_msg="info", log="admins"
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Удалена земля (каскад): {country} → {region}", kb)
    await cb.answer()

@router.callback_query(F.data == "admin:remove_city")
async def cb_remove_city(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.clear(); await _cancel_panel_timer(state); await ensure_config_exists()
    countries = await list_countries()
    if not countries:
        await _edit_panel(cb, state, "Стран нет.", _cancel_kb()); await cb.answer(); return
    kb = _make_country_kb("del_city", countries)
    await _edit_panel(cb, state, "Выберите СТРАНУ:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_city:country:"))
async def cb_del_city_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    country = cb.data.split("admin:del_city:country:", 1)[1]
    regions = await list_regions(country)
    if not regions:
        await _edit_panel(cb, state, f"В стране {country} нет земель.", _cancel_kb()); await cb.answer(); return
    kb = _make_region_kb("del_city", country, regions)
    await _edit_panel(cb, state, f"Страна: {country}\nВыберите ЗЕМЛЮ:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_city:region:"))
async def cb_del_city_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    payload = cb.data.split("admin:del_city:region:", 1)[1]
    country, region = payload.split("|", 1)
    cities = await list_cities(country, region)
    if not cities:
        await _edit_panel(cb, state, f"В {country} → {region} нет городов.", _cancel_kb()); await cb.answer(); return
    kb = _make_city_kb("del_city", country, region, cities)
    await _edit_panel(cb, state, f"{country} → {region}\nУдалить ГОРОД:", kb); await cb.answer()

@router.callback_query(F.data.startswith("admin:del_city:city:"))
async def cb_del_city_city(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    payload = cb.data.split("admin:del_city:city:", 1)[1]
    country, region, city = payload.split("|", 2)
    await _remove_city_sql_tree(country, region, city)
    await log_info(
        f"[admin {cb.from_user.id}] cities: remove city {country} → {region} → {city}",
        type_msg="info", log="admins"
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, f"✅ Удалён город: {country} → {region} → {city}", kb)
    await cb.answer()

@router.callback_query(
    (F.data.startswith("admin:pick_add_city:back:") | F.data.startswith("admin:del_city:back:")) & F.data.contains("|")
)
async def cb_back_from_city(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    payload = cb.data.split(":back:", 1)[1]
    # запасной предохранитель на всякий случай
    parts = payload.split("|", 1)
    if len(parts) != 2:
        # это не уровень "город", пусть обработает другой back-хэндлер
        await cb.answer()
        return
    country, region = parts
    regions = await list_regions(country)
    kb = _make_region_kb("pick_add_city" if "pick_add_city" in cb.data else "del_city", country, regions)
    await _edit_panel(cb, state, f"Страна: {country}\nВыберите ЗЕМЛЮ:", kb)
    await cb.answer("Назад")

# Назад из выбора ЗЕМЛИ → к списку стран (для сценариев add_city, del_city, add_region, del_region)
@router.callback_query(
    (F.data.startswith("admin:pick_add_city:back:") |
     F.data.startswith("admin:del_city:back:") |
     F.data.startswith("admin:pick_add_region:back:") |
     F.data.startswith("admin:del_region:back:")) & ~F.data.contains("|")
)
async def cb_back_from_region(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    # payload после back: "{country}" — но он нам не нужен для построения списка стран
    countries = await list_countries()
    prefix = (
        "pick_add_city" if "pick_add_city" in cb.data else
        "del_city" if "del_city" in cb.data else
        "pick_add_region" if "pick_add_region" in cb.data else
        "del_region"
    )
    kb = _make_country_kb(prefix, countries)
    await _edit_panel(cb, state, "Выберите СТРАНУ:", kb)
    await cb.answer("Назад")

# Назад из списка СТРАН → в ГЛАВНОЕ МЕНЮ (для тех же 4 сценариев)
@router.callback_query(
    F.data.in_((
        "admin:pick_add_city:back",
        "admin:del_city:back",
        "admin:pick_add_region:back",
        "admin:del_region:back",
    ))
)
async def cb_back_to_main(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await _turn_into_panel(cb.message, state, "Админ-панель")
    await cb.answer("Назад")

@router.callback_query(F.data == "admin:toggle_check_country")
async def cb_toggle_check_country(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    upd = await _toggle_check_country()
    await cb.answer(f"Проверка страны: {'Вкл' if upd.get('check_country') else 'Выкл'}")
    await log_info(
        f"[admin {cb.from_user.id}] toggle check_country → {bool(upd.get('check_country'))}",
        type_msg="info", log="admins"
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, "Админ-панель", kb)

@router.callback_query(F.data == "admin:toggle_region_in_bot")
async def cb_toggle_region_in_bot(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    upd = await _toggle_region_in_bot()
    await cb.answer(f"Земля в боте: {'Вкл' if upd.get('region_in_bot') else 'Выкл'}")
    await log_info(
        f"[admin {cb.from_user.id}] toggle region_in_bot → {bool(upd.get('region_in_bot'))}",
        type_msg="info", log="admins"
    )
    kb = await build_admin_kb()
    await _edit_panel_main_by_state(state, "Админ-панель", kb)

@router.callback_query(F.data == "admin:delete_user")
async def cb_delete_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.set_state(AdminStates.delete_user_id)
    await _cancel_panel_timer(state)
    await _delete_panel_safely(cb.message)
    prompt = await cb.message.answer("Введите user_id пользователя для удаления:", reply_markup=_cancel_kb(), disable_notification=True)
    await _save_prompt(state, prompt)
    await cb.answer()

@router.message(AdminStates.delete_user_id)
async def st_delete_user_id(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    text = (msg.text or "").strip()
    try:
        uid = int(text);  assert uid > 0
    except Exception:
        await _edit_panel_main(msg, state, "Нужен положительный целый user_id. Попробуйте ещё раз.", _cancel_kb())
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)   # ← удалить подсказку
        return

    from db.db_utils import get_user_data
    data = await get_user_data("users", uid)
    if not data:
        kb = await build_admin_kb()
        await _clear_state_preserve_panel(state)
        await _edit_panel_main_by_state(state, f"Пользователь user_id={uid} не найден.", kb)
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)   # ← удалить подсказку
        return

    await state.update_data(del_user_id=uid)
    card = _fmt_user_card(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить удаление", callback_data=f"admin:confirm_delete_user:{uid}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")]
    ])
    await _edit_panel_main(msg, state, f"Найден пользователь (user_id={uid}):\n\n{card}\n\nУдалить?", kb)
    try: await msg.delete()
    except: pass
    await _delete_saved_prompt(state)       # ← удалить подсказку

@router.callback_query(F.data.startswith("admin:confirm_delete_user:"))
async def cb_confirm_delete_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    uid_str = cb.data.split("admin:confirm_delete_user:", 1)[1]
    try:
        uid = int(uid_str)
    except Exception:
        await cb.answer("Некорректный id"); return
    ok = await _delete_user_sql(uid)
    if ok:
        main_bot = getattr(bot_instance, "bot", None)
        if not main_bot or not getattr(main_bot, "token", None):
            await log_info("notify deleted: main bot instance is missing or has no token", type_msg="warning", log="admins")
        else:
            try:
                await main_bot.send_message(
                    uid,
                    "Ваши учётные данные были удалены из нашей базы.\n"
                    "Вы можете снова начать пользоваться сервисом — отправьте /start.",
                    reply_markup=ReplyKeyboardRemove()
                )
            except Exception as e:
                await log_info(f"notify deleted user failed: {e}", type_msg="warning", log="admins")

    await log_info(
        f"[admin {cb.from_user.id}] delete user user_id={uid}: {'OK' if ok else 'FAILED'}",
        type_msg=("info" if ok else "warning"), log="admins"
    )
    kb = await build_admin_kb()
    await state.clear()
    result = '✅ Удалён' if ok else '❌ Не удалён'
    await _turn_into_panel(cb.message, state, f"{result} пользователь user_id={uid}")
    await cb.answer("Готово" if ok else "Ошибка")

@router.callback_query(F.data == "admin:block_user")
async def cb_block_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.set_state(AdminStates.block_user_id)
    await _cancel_panel_timer(state)
    await _delete_panel_safely(cb.message)
    prompt = await cb.message.answer("Введите user_id пользователя для блокировки:", reply_markup=_cancel_kb(), disable_notification=True)
    await _save_prompt(state, prompt)
    await cb.answer()

@router.message(AdminStates.block_user_id)
async def st_block_user_id(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    text = (msg.text or "").strip()
    try:
        uid = int(text);  assert uid > 0
    except Exception:
        await _edit_panel_main(msg, state, "Нужен положительный целый user_id. Попробуйте ещё раз.", _cancel_kb())
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)
        return

    from db.db_utils import get_user_data
    data = await get_user_data("users", uid)
    if not data:
        kb = await build_admin_kb()
        await _clear_state_preserve_panel(state)
        await _edit_panel_main_by_state(state, f"Пользователь user_id={uid} не найден.", kb)
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)
        return

    await state.update_data(block_user_id=uid)
    card = _fmt_user_card(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить блокировку", callback_data=f"admin:confirm_block_user:{uid}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")]
    ])
    await _edit_panel_main(msg, state, f"Найден пользователь (user_id={uid}):\n\n{card}\n\nЗаблокировать?", kb)
    try: await msg.delete()
    except: pass
    await _delete_saved_prompt(state)

@router.callback_query(F.data.startswith("admin:confirm_block_user:"))
async def cb_confirm_block_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    uid_str = cb.data.split("admin:confirm_block_user:", 1)[1]
    try:
        uid = int(uid_str)
    except Exception:
        await cb.answer("Некорректный id"); return

    ok = await _block_user_sql(uid)
    await log_info(
        f"[admin {cb.from_user.id}] block user user_id={uid}: {'OK' if ok else 'FAILED'}",
        type_msg=("info" if ok else "warning"), log="admins"
    )

    # Уведомляем юзера основным ботом (не info_bot)
    if ok:
        main_bot = getattr(bot_instance, "bot", None)
        if not main_bot or not getattr(main_bot, "token", None):
            await log_info("notify blocked: main bot instance is missing or has no token", type_msg="warning", log="admins")
        else:
            try:
                await main_bot.send_message(
                    uid,
                    "Ваш доступ к сервису временно заблокирован.\n"
                    "Если Вы считаете это ошибкой — напишите в поддержку командой /support.",
                    reply_markup=ReplyKeyboardRemove()
                )
            except Exception as e:
                await log_info(f"notify blocked user failed: {e}", type_msg="warning", log="admins")

    # ↓↓↓ ДОБАВЬ ЭТО
    kb = await build_admin_kb()
    await state.clear()
    result = '✅ Заблокирован' if ok else '❌ Не заблокирован'
    await _turn_into_panel(cb.message, state, f"{result} пользователь user_id={uid}")
    await cb.answer("Готово" if ok else "Ошибка")

@router.callback_query(F.data == "admin:unblock_user")
async def cb_unblock_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    await state.set_state(AdminStates.unblock_user_id)
    await _cancel_panel_timer(state)
    await _delete_panel_safely(cb.message)
    # одноразовая подсказка, которую потом удалим
    prompt = await cb.message.answer("Введите user_id пользователя для разблокировки:", reply_markup=_cancel_kb(), disable_notification=True)
    await _save_prompt(state, prompt)
    await cb.answer()

@router.message(AdminStates.unblock_user_id)
async def st_unblock_user_id(msg: Message, state: FSMContext):
    if not _allowed_place(msg): return
    text = (msg.text or "").strip()
    try:
        uid = int(text);  assert uid > 0
    except Exception:
        await _edit_panel_main(msg, state, "Нужен положительный целый user_id. Попробуйте ещё раз.", _cancel_kb())
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)
        return

    from db.db_utils import get_user_data
    data = await get_user_data("users", uid)
    if not data:
        kb = await build_admin_kb()
        await _clear_state_preserve_panel(state)
        await _edit_panel_main_by_state(state, f"Пользователь user_id={uid} не найден.", kb)
        try: await msg.delete()
        except: pass
        await _delete_saved_prompt(state)
        return

    await state.update_data(unblock_user_id=uid)
    card = _fmt_user_card(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить разблокировку", callback_data=f"admin:confirm_unblock_user:{uid}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")]
    ])
    await _edit_panel_main(msg, state, f"Найден пользователь (user_id={uid}):\n\n{card}\n\nРазблокировать?", kb)
    try: await msg.delete()
    except: pass
    await _delete_saved_prompt(state)

@router.callback_query(F.data.startswith("admin:confirm_unblock_user:"))
async def cb_confirm_unblock_user(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message): return
    uid_str = cb.data.split("admin:confirm_unblock_user:", 1)[1]
    try:
        uid = int(uid_str)
    except Exception:
        await cb.answer("Некорректный id"); return

    ok = await _unblock_user_sql(uid)
    await log_info(
        f"[admin {cb.from_user.id}] unblock user user_id={uid}: {'OK' if ok else 'FAILED'}",
        type_msg=("info" if ok else "warning"), log="admins"
    )

    # (необязательно, но удобно) уведомим юзера основным ботом, что доступ восстановлен
    if ok:
        main_bot = getattr(bot_instance, "bot", None)
        if main_bot and getattr(main_bot, "token", None):
            try:
                kb = await reply_keyboard(uid)
                await main_bot.send_message(
                    uid,
                    "Ваш доступ к сервису восстановлен. Добро пожаловать обратно!",
                    reply_markup=kb
                )
            except Exception as e:
                await log_info(f"notify unblocked user failed: {e}", type_msg="warning", log="admins")

    kb = await build_admin_kb()
    await state.clear()
    result = '✅ Разблокирован' if ok else '❌ Не разблокирован'
    await _turn_into_panel(cb.message, state, f"{result} пользователь user_id={uid}")
    await cb.answer("Готово" if ok else "Ошибка")

@router.callback_query(F.data == "admin:export_config")
async def cb_export_config(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    cfg = await get_all_config()
    text = _human_readable_config(cfg)
    try:
        cities = cfg.get("cities") or {}
        n_countries = len(cities) if isinstance(cities, dict) else 0
        n_regions = 0
        n_cities = 0
        if isinstance(cities, dict):
            for _regions in cities.values():
                if isinstance(_regions, dict):
                    n_regions += len(_regions)
                    for _lst in _regions.values():
                        if isinstance(_lst, list):
                            n_cities += len(_lst)
        await log_info(
            f"[admin {cb.from_user.id}] export config: countries={n_countries}, regions={n_regions}, cities={n_cities}, "
            f"stars_enabled={bool(cfg.get('stars_enabled', False))}, check_country={bool(cfg.get('check_country', False))}, "
            f"region_in_bot={bool(cfg.get('region_in_bot', True))}",
            type_msg="info", log="admins"
        )
    except Exception:
        # не мешаем экрану экспорта, если подсчёт не удался
        pass
    kb = _export_kb()
    await _edit_panel_main(cb, state, text, kb)  # редактируем текущий пост
    await cb.answer()

@router.callback_query(F.data == "admin:export_config:back")
async def cb_export_config_back(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    await _turn_into_panel(cb.message, state, "Админ-панель")
    await cb.answer("Назад")

@router.callback_query(F.data == "admin:export_config:cancel")
async def cb_export_config_cancel(cb: CallbackQuery, state: FSMContext):
    if not _allowed_place(cb.message):
        return
    # просто удаляем текущий экран экспорта
    try:
        await cb.message.delete()
    except Exception:
        pass
    # чистим только временное состояние (если вдруг было), панель не создаём
    await _cancel_state_timer(state)
    await cb.answer("Закрыто")