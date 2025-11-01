from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from nicegui import ui, app
from web.web_start_reg_form import start_reg_form_ui
from web.web_utilits import (
    DEFAULT_AVATAR_DATA_URL,
    bind_enter_action,
    fetch_telegram_avatar,
)

from log.log import log_info
from config.config import SUPPORTED_LANGUAGE_NAMES, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES
from config.config_utils import lang_dict
from db.db_utils import delete_user, update_table

SECTION_TITLE_KEYS: dict[str, str] = {
    "personal_data": "profile_personal_title",
    "car_data": "profile_driver_vehicle_title",
    "balance": "profile_balance_title",
    "settings": "profile_settings_title",
    "support": "profile_support_title",
}

MAX_FAVORITE_ADDRESSES: int = 5
AVATAR_CACHE_TTL_SEC: float = 300.0  # TTL кеша аватаров (в секундах)

RenderFn = Callable[[ui.element], Awaitable[None]] 

ActionHandler = Callable[[Any | None], Awaitable[None]]
BodyRenderer = Callable[[ui.element], Awaitable[None]]

active_tab: str = 'profile_list'


async def profile_menu(
    uid: int | None,
    user_lang: str,
    user_data: dict | None,
) -> None:
    """Render the profile tab UI with dynamic re-render support."""
    async def _log(message: str, *, type_msg: str) -> None:
        # Локальный помощник для логирования с привязкой текущего uid
        await log_info(message, type_msg=type_msg, uid=uid)
    # === Общая обёртка рендера профиля и проверка входных данных ===
    try:
        await _log("[profile_menu] render start", type_msg="info")

        # === Сценарий отсутствия данных пользователя в профиле ===
        if (not uid) or (user_data is None) or (user_data.get("phone_passenger") is None):
            with ui.column().classes(
                "w-full gap-3 q-pa-xl items-center justify-center text-center"
            ):
                ui.icon("person_off").props("size=72px color=grey-5")
                ui.label(lang_dict("profile_empty_title", user_lang)).classes(
                    "text-h6"
                )
                ui.label(lang_dict("profile_empty_hint", user_lang)).classes(
                    "text-body2"
                )

                async def _retry() -> None:
                    try:
                        await _log(
                            "[profile_menu] retry clicked", type_msg="info"
                        )
                        await ui.run_javascript("location.reload()")
                    except Exception as retry_error:
                        await _log(
                            f"[profile_menu][retry][ОШИБКА] {retry_error!r}",
                            type_msg="error",
                        )
                        ui.notify(
                            lang_dict("profile_retry_error", user_lang),
                            type="negative",
                        )
                        raise

                ui.button(
                    lang_dict("profile_retry", user_lang),
                    on_click=_retry,
                ).props("color=primary unelevated")

            await _log("[profile_menu] render complete", type_msg="info")
            return None
        user: dict[str, Any] = dict(user_data)
        user_deleted: bool = False

        # === Нормализация адресов и транзакций из БД ===

        def _load_addresses(raw: object) -> dict[str, Any]:
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}
            return {}

        def _load_transactions(raw: object) -> list[Any]:
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
            return []

        user["addresses_passenger"] = _load_addresses(
            user.get("addresses_passenger")
        )
        user["transactions"] = _load_transactions(user.get("transactions"))

        # === Получение и кеширование аватара пользователя ===
        # Обновляем кеш аватара, если срок действия истёк или в кеше лежит заглушка
        tg_photo_url: str | None = app.storage.user.get("tg_photo_url")
        cached_ts_raw: object = app.storage.user.get("tg_photo_url_checked_at")

        try:
            cached_ts: float | None = (
                float(cached_ts_raw) if cached_ts_raw is not None else None
            )
        except (TypeError, ValueError):
            cached_ts = None

        now_ts: float = time.monotonic()
        needs_refresh: bool = False

        if tg_photo_url in (None, ""):
            needs_refresh = True
        elif cached_ts is None or (now_ts - cached_ts) >= AVATAR_CACHE_TTL_SEC:
            needs_refresh = True

        if needs_refresh and uid:
            try:
                await _log(
                    "[profile_menu][avatar] обновление из Bot API",
                    type_msg="info",
                )
                tg_photo_url = await fetch_telegram_avatar(uid)
            except Exception as avatar_error:
                await _log(
                    f"[profile_menu][avatar][ОШИБКА] {avatar_error!r}",
                    type_msg="error",
                )
                if tg_photo_url in (None, ""):
                    tg_photo_url = DEFAULT_AVATAR_DATA_URL
            finally:
                app.storage.user["tg_photo_url_checked_at"] = now_ts

        if tg_photo_url not in (None, ""):
            app.storage.user["tg_photo_url"] = tg_photo_url

        has_real_avatar: bool = tg_photo_url not in (
            None,
            "",
            DEFAULT_AVATAR_DATA_URL,
        )

        avatar_url = (
            tg_photo_url if has_real_avatar else DEFAULT_AVATAR_DATA_URL
        )

        # === Текущее состояние локали и роли ===
        current_lang = user_lang or "en"
        current_role = (user.get("role") or "passenger").lower()

        def _format_rating(value: object) -> str:
            try:
                numeric = float(value or 0)
                return f"{numeric:.1f}"
            except (TypeError, ValueError):
                return "0.0"

        def _role_display(role: str | None, lang: str) -> str:
            role_key = (role or "").lower()
            if role_key == "driver":
                return lang_dict("profile_role_driver", lang)
            if role_key == "passenger":
                return lang_dict("profile_role_passenger", lang)
            return lang_dict("profile_role_unknown", lang)

        def _other_role(role: str) -> str:
            return "driver" if role == "passenger" else "passenger"

        def _role_switch_label(role: str, lang: str) -> str:
            return lang_dict(
                "profile_role_switch_to_driver"
                if role == "passenger"
                else "profile_role_switch_to_passenger",
                lang,
            )

        def _display_value(value: str | None, lang: str) -> str:
            return value if value else lang_dict("profile_value_missing", lang)

        def _merge_user(updates: dict[str, Any]) -> None:
            user.update(updates)


        def _normalize_address_map(raw: object) -> dict[str, dict[str, str]]:
            """Приводим адреса к единому виду {id: {address, address_name}}."""
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {}
            if not isinstance(raw, Mapping):
                return {}

            normalized: dict[str, dict[str, str]] = {}
            for key, value in raw.items():
                address_id = str(key)
                address_value = ""
                address_name = ""

                if isinstance(value, Mapping):
                    address_value = str(value.get("address") or "").strip()
                    address_name = str(value.get("address_name") or "").strip()
                else:
                    address_value = str(value or "").strip()

                if not address_value:
                    continue

                normalized[address_id] = {
                    "address": address_value,
                    "address_name": address_name,
                }

            return normalized


        def _fallback_address_name(address_id: str, lang: str) -> str:
            """Возвращает локализованную подпись для стандартных ключей."""
            key = address_id.lower()
            if key == "home":
                return lang_dict("profile_address_home_title", lang)
            if key == "work":
                return lang_dict("profile_address_work_title", lang)
            return lang_dict("profile_address_favorite_title", lang)


        def _address_display_name(address_id: str, data: Mapping[str, Any], lang: str) -> str:
            """Определяет подпись адреса для отображения."""
            saved_name = str(data.get("address_name") or "").strip()
            if saved_name:
                return saved_name
            return _fallback_address_name(address_id, lang)


        # Корневой контейнер профиля, который будем очищать при смене языка/данных
        tabs = (ui.tabs(value='profile_list')
                .props('dense no-caps align=justify narrow-indicator '
                       'active-color=primary indicator-color=primary')
                .classes('hidden w-full')
        )
        root = ui.column().classes("w-full q-pa-none q-pt-none q-gutter-y-none").style(
                # занимаем весь экран; dvh корректно работает на мобилках
                "min-height:100dvh; height:100dvh; max-height:100dvh;"
                # на всю ширину без горизонтального скролла
                "width:100%; max-width:100%;"
                # flex-колонка и управление прокруткой содержимого
                "display:flex; flex-direction:column; overflow-y:auto; overflow-x:hidden;"
            )

        registration_dialog: ui.dialog | None = None
        reg_container: Any | None = None

        async def _apply_theme_js(mode: str) -> None:
            """Применяет тему через готовую реализацию на фронте."""
            try:
                js = f"""
if (window.__syncing_theme_toggle) return;
const desired = '{mode}';
const dark = desired === 'dark';
try {{ window.Quasar?.Dark?.set?.(dark); }} catch {{}}
const body = document.body;
body.classList.toggle('body--dark', dark);
body.classList.toggle('body--light', !dark);
const bg = dark ? '#0b0b0c' : '#ffffff';
try {{
  document.documentElement.style.setProperty('color-scheme', dark ? 'dark' : 'light');
  document.documentElement.style.backgroundColor = bg;
  body.style.backgroundColor = bg;
  window.Telegram?.WebApp?.setBackgroundColor?.(bg);
}} catch {{}}
try {{
  const inputs = document.querySelectorAll('.q-field__native, .q-field__input');
  inputs.forEach(el => {{ el.style.webkitTextFillColor = getComputedStyle(el).color; }});
}} catch {{}}
const uid = window.Telegram?.WebApp?.initDataUnsafe?.user?.id || localStorage.getItem('tg_user_id');
if (uid) {{
  const blob = new Blob([JSON.stringify({{ user_id: uid, theme: desired }})], {{type: 'application/json'}});
  navigator.sendBeacon('/api/theme', blob);
}}
"""
                await ui.run_javascript(js)
            except Exception as theme_error:
                await _log(
                    f"[profile_menu][theme_js][ОШИБКА] {theme_error!r}",
                    type_msg="warning",
                )

        async def _open_registration() -> None:
            """Открывает диалог регистрации при нехватке данных."""
            nonlocal registration_dialog, reg_container, current_lang
            if registration_dialog is None or reg_container is None:
                return
            try:
                source_tab = active_tab
                registration_dialog.open()
                reg_container.clear()

                async def _close_registration() -> None:
                    try:
                        registration_dialog.close()
                        reg_container.clear()
                        await _log("[profile_menu][reg_form][CLOSE] dialog closed", type_msg="info")

                        # КРИТИЧНО: «реармим» on_value_change — переводим на нейтральный таб
                        try:
                            tabs.set_value("profile_list")
                            app.storage.user["panel"] = "profile"
                            app.storage.user["nav"] = "profile"
                            await _log("[profile_menu][reg_form][CLOSE] tabs -> profile_list", type_msg="info")
                        except Exception as e:
                            await _log(f"[profile_menu][reg_form][CLOSE][tabs_set][ОШИБКА] {e!r}", type_msg="error")
                            # не падать — это вспомогательная операция
                    except Exception as close_error:
                        await _log(f"[profile_menu][reg_form][CLOSE][ОШИБКА] {close_error!r}", type_msg="error")


                with reg_container:
                    # Верхняя панель с «X» справа
                    with ui.row().classes(
                        "w-full items-center no-wrap q-pa-none q-mb-none"
                    ):
                        ui.space()
                        ui.button(
                            icon="close",
                            on_click=_close_registration,
                        ).props("flat round dense size=md color=grey-6").classes("q-mr-none")
                        # Если нужен tooltip и есть ключ в словаре, раскомментируй:
                        # .tooltip(lang_dict("dialog_close", current_lang))

                    # Контент формы регистрации
                    await start_reg_form_ui(
                        uid, current_lang, user, choice_role=False
                    )

            except Exception as reg_error:
                await _log(
                    f"[profile_menu][reg_form][ОШИБКА] {reg_error!r}",
                    type_msg="error",
                )
                registration_dialog.close()
                raise

        async def _switch_role() -> bool:
            """Переключаем роль и обновляем состояние профиля."""
            nonlocal current_role
            try:
                if not uid:
                    return False
                new_role = _other_role(current_role)
                if new_role == "driver":
                    required_fields = (
                        "car_brand",
                        "car_model",
                        "car_color",
                        "car_number",
                        "car_image",
                        "techpass_image",
                    )
                    missing = [f for f in required_fields if not user.get(f)]
                    if missing:
                        await _log(
                            "[profile_menu] driver switch requires registration",
                            type_msg="info",
                        )
                        ui.notify(
                            lang_dict(
                                "profile_role_switch_error", current_lang
                            ),
                            type="warning",
                        )
                        await _open_registration()
                        return False

                if not await update_table("users", uid, {"role": new_role}):
                    ui.notify(
                        lang_dict("profile_role_switch_error", current_lang),
                        type="negative",
                    )
                    return False

                _merge_user({"role": new_role})
                current_role = new_role
                await _log(
                    f"[profile_menu] role switched -> {new_role}",
                    type_msg="info",
                )
                ui.notify(
                    lang_dict(
                        "profile_role_switch_success",
                        current_lang,
                        role=_role_display(new_role, current_lang),
                    ),
                    type="positive",
                )

                panels_component = app.storage.client.get("tab_panels")
                if panels_component is not None:
                    panels_component.set_value("profile")
                app.storage.user["panel"] = "profile"
                app.storage.user["nav"] = "profile"

                return True
            except Exception as switch_error:
                await _log(
                    f"[profile_menu][switch_role][ОШИБКА] {switch_error!r}",
                    type_msg="error",
                )
                ui.notify(
                    lang_dict("profile_role_switch_error", current_lang),
                    type="negative",
                )
                raise

        def _attach_panel_click(panel: Any, target: str) -> None:
            """Подключаем клик по панели к скрытым табам."""

            async def _on_panel_click(_: Any) -> None:
                try:
                    tabs.set_value(target)
                except Exception as click_error:
                    await _log(
                        f"[profile_menu][panel_click][ОШИБКА] {click_error!r}",
                        type_msg="error",
                    )
                    raise

            panel.on("click", _on_panel_click)

        async def _go_back_to_profile() -> None:
            """Возвращаемся на корневую карточку профиля."""
            try:
                await _log(
                    "[profile_menu] возврат на корневой экран",
                    type_msg="info",
                )
                tabs.set_value("profile_list")
            except Exception as back_error:
                await _log(
                    f"[profile_menu][back][ОШИБКА] {back_error!r}",
                    type_msg="error",
                )
                raise
  
        #================================================================
        # Отрисовка секций профиля
        #================================================================

        def _kv(
            icon_name: str | None,
            label_key: str,
            value: str | None,
            on_click: Callable[[Any], Awaitable[None]] | None = None,
        ) -> ui.element:
            """Одна строка 'Иконка — Значение ..... Подпись'. Возвращает row; опционально навешивает клик."""
            row = ui.row().classes("w-full items-center no-wrap q-px-none cursor-pointer")
            with row:
                if icon_name:
                    ui.icon(icon_name).classes("text-body1")
                ui.label(_display_value(value, current_lang)).classes("text-body1")
                ui.space()
                ui.label(lang_dict(label_key, current_lang)).classes('text-caption text-grey-6')
            if on_click:
                row.on("click", on_click)
            return row

        async def _render_personal(content: ui.element) -> None:
            try:
                with content:
                    with ui.card().classes('w-full q-pa-md gap-2'):
                        async def _open_phone_dialog(_: Any | None = None) -> None:
                            try:
                                await _open_input_dialog(
                                    initial_value=user.get("phone_passenger"),
                                    label_key="profile_phone_edit",
                                    action_key="profile_phone_save",
                                    updates_factory=lambda phone, _second=None: {
                                        "phone_passenger": phone,
                                        "phone_driver": phone,
                                    },
                                    section="personal_data",
                                    ok_key="profile_phone_updated",
                                    err_key="profile_phone_update_error",
                                )
                            except Exception as dialog_error:
                                await _log(
                                    f"[profile_menu][phone][dialog][ОШИБКА] {dialog_error!r}",
                                    type_msg="error",
                                )
                                raise
                        _kv("smartphone", "profile_phone_edit", user.get("phone_passenger"), on_click=_open_phone_dialog)

                    with ui.card().classes('w-full q-pa-md gap-2'):
                        # Работаем со списком избранных адресов.
                        addresses_map = _normalize_address_map(user.get('addresses_passenger'))

                        ui.label(lang_dict('profile_addresses_title', current_lang)).classes('text-h6')
                        ui.separator()

                        async def _open_address_dialog(address_id: str | None, _: Any | None = None) -> None:
                            """Открываем диалог для добавления или редактирования адреса."""
                            pending_addresses: dict[str, dict[str, str]] | None = None
                            try:
                                is_edit = address_id is not None
                                has_target = bool(is_edit and address_id in addresses_map)
                                if (not is_edit) and len(addresses_map) >= MAX_FAVORITE_ADDRESSES:
                                    await _log(
                                        f"[profile_menu][address] limit reached ({MAX_FAVORITE_ADDRESSES})",
                                        type_msg="warning",
                                    )
                                    ui.notify(
                                        lang_dict(
                                            'profile_address_limit_reached',
                                            current_lang,
                                            count=str(MAX_FAVORITE_ADDRESSES),
                                        ),
                                        type="warning",
                                    )
                                    return

                                existing = addresses_map.get(address_id or "", {})
                                initial_address = existing.get("address") if existing else ""
                                initial_name = existing.get("address_name") or (
                                    _fallback_address_name(address_id or "", current_lang)
                                    if address_id
                                    else ""
                                )

                                def _updates_factory(address_value: str, name_value: str | None) -> dict[str, Any]:
                                    nonlocal pending_addresses
                                    addr_clean = address_value.strip()
                                    name_clean = (name_value or "").strip()

                                    if not addr_clean or not name_clean:
                                        raise ValueError('profile_address_required')

                                    target_id = address_id or uuid4().hex[:8]
                                    updated = dict(addresses_map)
                                    updated[target_id] = {
                                        "address": addr_clean,
                                        "address_name": name_clean,
                                    }
                                    pending_addresses = updated
                                    return {
                                        "addresses_passenger": json.dumps(updated, ensure_ascii=False)
                                    }

                                async def _post_merge() -> None:
                                    """Обновляем локальный кэш пользователя после успешного сохранения."""
                                    nonlocal addresses_map
                                    if pending_addresses is None:
                                        return
                                    try:
                                        user['addresses_passenger'] = pending_addresses
                                        addresses_map = dict(pending_addresses)
                                    except Exception as state_error:
                                        await _log(
                                            f"[profile_menu][address][state][ОШИБКА] {state_error!r}",
                                            type_msg="warning",
                                        )

                                async def _delete_address() -> None:
                                    """Удаляем текущий адрес пользователя."""
                                    nonlocal pending_addresses
                                    try:
                                        if not has_target or not address_id:
                                            ui.notify(
                                                lang_dict('profile_address_delete_error', current_lang),
                                                type='negative',
                                            )
                                            await _log(
                                                "[profile_menu][address][delete] адрес не найден",
                                                type_msg='warning',
                                            )
                                            return

                                        updated = dict(addresses_map)
                                        if address_id not in updated:
                                            ui.notify(
                                                lang_dict('profile_address_delete_error', current_lang),
                                                type='negative',
                                            )
                                            await _log(
                                                "[profile_menu][address][delete] запись отсутствует в кеше",
                                                type_msg='warning',
                                            )
                                            return

                                        updated.pop(address_id, None)
                                        pending_addresses = updated

                                        await _save(
                                            updates={
                                                'addresses_passenger': json.dumps(updated, ensure_ascii=False)
                                            },
                                            section='personal_data',
                                            ok_key='profile_address_deleted',
                                            err_key='profile_address_delete_error',
                                            on_after_ok=_post_merge,
                                            merge_state=False,
                                        )
                                    except Exception as delete_error:
                                        await _log(
                                            f"[profile_menu][address][delete][ОШИБКА] {delete_error!r}",
                                            type_msg='error',
                                        )
                                        ui.notify(
                                            lang_dict('profile_address_delete_error', current_lang),
                                            type='negative',
                                        )
                                        raise

                                await _open_input_dialog(
                                    initial_value=initial_address,
                                    initial_value_2=initial_name,
                                    label_key='profile_address_value_edit',
                                    label_key_2='profile_address_name_edit',
                                    action_key='profile_address_save',
                                    updates_factory=_updates_factory,
                                    section='personal_data',
                                    ok_key='profile_address_updated',
                                    err_key='profile_address_update_error',
                                    additional=True,
                                    merge_state=False,
                                    delete_key='profile_address_delete' if has_target else None,
                                    delete_handler=_delete_address if has_target else None,
                                    post_merge=_post_merge,
                                )
                            except Exception as dialog_error:
                                await _log(
                                    f"[profile_menu][address][dialog][ОШИБКА] {dialog_error!r}",
                                    type_msg="error",
                                )
                                raise

                        def _make_address_handler(address_id: str | None) -> Callable[[Any | None], Awaitable[None]]:
                            async def _handler(_: Any | None = None) -> None:
                                await _open_address_dialog(address_id)
                            return _handler

                        with ui.column().classes('w-full q-gutter-y-sm'):
                            if addresses_map:
                                for address_id, address_data in addresses_map.items():
                                    display_value = (
                                        f"{_address_display_name(address_id, address_data, current_lang)}: "
                                        f"{address_data.get('address', '')}"
                                    )
                                    _kv(
                                        'place',
                                        'profile_address_edit',
                                        display_value,
                                        on_click=_make_address_handler(address_id),
                                    )

                            if len(addresses_map) < MAX_FAVORITE_ADDRESSES:
                                _kv(
                                    'add',
                                    'profile_address_edit',
                                    lang_dict('profile_address_add', current_lang),
                                    on_click=_make_address_handler(None),
                                )
                            else:
                                ui.label(
                                    lang_dict(
                                        'profile_address_limit_reached',
                                        current_lang,
                                        count=str(MAX_FAVORITE_ADDRESSES),
                                    )
                                ).classes('text-caption text-grey-6')
            except Exception as e:
                await _log(f"[profile_menu][personal][ОШИБКА] {e!r}", type_msg="error")
                raise

        async def _render_car(content: ui.element) -> None:
            try:
                with content:
                    with ui.card().classes('w-full q-pa-md gap-2'):
                        _kv('profile_car_brand', user.get('car_brand'))
                        _kv('profile_car_model', user.get('car_model'))
                        _kv('profile_car_color', user.get('car_color'))
                        _kv('profile_car_number', user.get('car_number'))
                        # фото
                        imgs = [user.get('car_image'), user.get('techpass_image')]
                        imgs = [x for x in imgs if x]
                        if imgs:
                            ui.separator()
                            with ui.row().classes('gap-2 no-wrap'):
                                for src in imgs:
                                    ui.image(src).classes('w-40 h-32').style('object-fit:cover;border-radius:12px;')
                        else:
                            ui.label(lang_dict('profile_no_car_images', current_lang)).classes('text-body2 text-grey')
            except Exception as e:
                await _log(f'[profile_menu][car][ОШИБКА] {e!r}', type_msg='error')
                raise

        async def _render_balance(content: ui.element) -> None:
            try:
                with content:
                    with ui.card().classes('w-full q-pa-md gap-2'):
                        bal = user.get('balance')
                        ui.label(lang_dict('profile_balance_current', current_lang, value=str(bal or 0))).classes('text-h6')
                        txs = user.get('transactions') or []
                        if not txs:
                            ui.label(lang_dict('profile_no_transactions', current_lang)).classes('text-body2 text-grey')
                        else:
                            with ui.column().classes('w-full q-gutter-y-sm'):
                                for t in txs[:10]:
                                    # ожидаем t: {"amount": 10.5, "title": "...", "created_at": "..."}
                                    title = str((t or {}).get('title') or lang_dict('profile_txn', current_lang))
                                    amount = str((t or {}).get('amount') or '0')
                                    dt = str((t or {}).get('created_at') or '')
                                    ui.label(f'{title} — {amount} — {dt}').classes('text-body2')
            except Exception as e:
                await _log(f'[profile_menu][balance][ОШИБКА] {e!r}', type_msg='error')
                raise

        async def _render_settings(content: ui.element) -> None:
            try:
                with content:

                    # Карточка выбора языка интерфейса
                    supported_codes_raw = [
                        str(code).lower()
                        for code in (SUPPORTED_LANGUAGES or [])
                        if isinstance(code, str) and code.strip()
                    ]
                    fallback_lang = (DEFAULT_LANGUAGES or 'en').lower()

                    unique_codes: list[str] = []
                    # Гарантируем, что текущий и дефолтный языки присутствуют в выпадающем списке
                    for candidate in [*supported_codes_raw, current_lang, fallback_lang]:
                        if candidate and candidate not in unique_codes:
                            unique_codes.append(candidate)

                    language_name_map = SUPPORTED_LANGUAGE_NAMES if isinstance(SUPPORTED_LANGUAGE_NAMES, dict) else {}
                    language_options = {
                        code: str(language_name_map.get(code) or code.upper())
                        for code in unique_codes
                    }

                    language_labels = dict(language_options)
                    initial_lang: str | None = None
                    if language_options:
                        if current_lang in language_labels:
                            initial_lang = current_lang
                        elif fallback_lang in language_labels:
                            initial_lang = fallback_lang
                        else:
                            initial_lang = next(iter(language_options))

                    with ui.card().classes('w-full q-pa-md gap-3'):
                        ui.label(lang_dict('profile_language_label', current_lang)).classes('text-subtitle1')
                        lang_select = (
                            ui.select(
                                language_options,
                                value=initial_lang,
                                with_input=False,
                            )
                            .props('outlined dense')
                            .classes('w-full')
                        )

                        if not language_options:
                            lang_select.disable()
                            lang_select.value = None

                        async def _handle_language_change(e: ui.events.ValueChangeEventArguments) -> None:
                            client = e.client
                            try:
                                # Запоминаем клиент, чтобы создавать уведомления и таймеры вне устаревших слотов
                                new_lang = str(e.value or '').lower()
                                if not new_lang or new_lang == current_lang:
                                    return
                                if new_lang not in language_labels:
                                    if client is not None:
                                        with client:
                                            ui.notify(
                                                lang_dict('profile_language_update_error', current_lang),
                                                type='negative',
                                            )
                                    return
                                if not await update_table('users', uid, {'language': new_lang}):
                                    if client is not None:
                                        with client:
                                            ui.notify(
                                                lang_dict('profile_language_update_error', current_lang),
                                                type='negative',
                                            )
                                    return

                                user['language'] = new_lang
                                app.storage.user['lang'] = new_lang
                                await _log(
                                    f"[profile_menu][settings][язык] выбран новый язык: {new_lang}",
                                    type_msg='info',
                                )

                                if client is not None:
                                    with client:
                                        ui.notify(
                                            lang_dict(
                                                'profile_language_updated',
                                                new_lang,
                                                language=language_labels.get(new_lang, new_lang.upper()),
                                            ),
                                            type='positive',
                                        )

                                await _render(new_lang)

                                target_tab = app.storage.user.get('panel') or 'profile'
                                # Отложенно перерисовываем основное приложение, чтобы обновить подписи
                                if client is not None:
                                    with client:
                                        ui.timer(
                                            2,
                                            lambda: ui.navigate.to(f"/main_app?tab={target_tab}"),
                                            once=True,
                                        )

                            except Exception as lang_error:
                                await _log(
                                    f"[profile_menu][settings][язык][ОШИБКА] {lang_error!r}",
                                    type_msg='error',
                                )
                                if client is not None:
                                    with client:
                                        ui.notify(
                                            lang_dict('profile_language_update_error', current_lang),
                                            type='negative',
                                        )
                                raise

                        lang_select.on_value_change(_handle_language_change)

                    # Карточка переключения темы оформления
                    with ui.card().classes('w-full q-pa-md gap-3'):
                        ui.label(lang_dict('profile_theme_label', current_lang)).classes('text-subtitle1')
                        current_theme_is_dark = (user.get('theme_mode') or 'light') == 'dark'
                        theme_toggle = ui.toggle(
                            {
                                'light': lang_dict('profile_theme_light', current_lang),
                                'dark': lang_dict('profile_theme_dark', current_lang),
                            },
                            value='dark' if current_theme_is_dark else 'light',
                        ).props('dense flatemit-value')

                        async def _on_theme_change(e: ui.events.ValueChangeEventArguments) -> None:
                            try:
                                mode = str(e.value or '').lower()
                                if mode not in {'light', 'dark'}:
                                    ui.notify(lang_dict('profile_theme_save_error', current_lang), type='negative')
                                    return

                                if not await update_table('users', uid, {'theme_mode': mode}):
                                    ui.notify(lang_dict('profile_theme_save_error', current_lang), type='negative')
                                    return

                                await _apply_theme_js(mode)
                                # Обновляем локальный кэш пользователя, чтобы тумблер показывал актуальное состояние
                                user['theme_mode'] = mode
                                app.storage.user['theme_mode'] = mode
                                ui.notify(lang_dict('profile_theme_saved', current_lang), type='positive')
                                await _log(
                                    f"[profile_menu][settings][тема] переключение на режим: {mode}",
                                    type_msg='info',
                                )

                            except Exception as err:
                                await _log(
                                    f"[profile_menu][settings][тема][ОШИБКА] {err!r}",
                                    type_msg='error',
                                )
                                ui.notify(lang_dict('profile_theme_save_error', current_lang), type='negative')
                                raise

                        theme_toggle.on_value_change(_on_theme_change)
            except Exception as e:
                await _log(f'[profile_menu][settings][ОШИБКА] {e!r}', type_msg='error')
                raise

        async def _render_support(content: ui.element) -> None:
            try:
                with content:
                    with ui.card().classes('w-full q-pa-md gap-2'):
                        ui.label(lang_dict('profile_support_hint', current_lang)).classes('text-body2')
                        ui.button(lang_dict('profile_support_contact', current_lang)).props('color=primary unelevated')
            except Exception as e:
                await _log(f'[profile_menu][support][ОШИБКА] {e!r}', type_msg='error')
                raise


        SECTION_RENDERERS: dict[str, RenderFn] = {
            'personal_data': _render_personal,
            'car_data': _render_car,
            'balance': _render_balance,
            'settings': _render_settings,
            'support': _render_support,
        }


        async def _render_section_stub(section: str) -> None:
            """Рисуем пустую страницу с кнопкой Back."""
            try:
                root.clear()
                title_key = SECTION_TITLE_KEYS.get(
                    section, "profile_personal_title"
                )
                with root:
                    with ui.column().classes("w-full gap-4 q-pa-lg"):
                        with ui.row().classes("w-full items-center").style('position: relative;'):
                            ui.button(
                                icon="arrow_back",
                                on_click=_go_back_to_profile,
                            ).props("color=primary flat")
                            ui.label(
                                lang_dict(title_key, current_lang),
                            ).classes("text-h6 text-center").style('position:absolute; left:50%; transform:translateX(-50%);')
                    content = ui.column().classes('w-full q-gutter-y-md')

                    renderer = SECTION_RENDERERS.get(section)
                    if renderer is not None:
                        await renderer(content)
                    else:
                        with content:
                            ui.label(lang_dict('profile_section_empty', current_lang)).classes('text-body2 text-grey')

                await _log(
                    f"[profile_menu] секция {section} открыта",
                    type_msg="info",
                )
            except Exception as stub_error:
                await _log(
                    f"[profile_menu][section_stub][ОШИБКА] {stub_error!r}",
                    type_msg="error",
                )
                raise

        
        async def _save(
            *,
            updates: dict[str, Any],
            section: str,
            ok_key: str,
            err_key: str,
            on_after_ok: Callable[[], Awaitable[None]] | None = None,
            merge_state: bool = True,
        ) -> bool:
            """Единая запись в БД (users) + notify + мягкий re-render + опц. внешний колбэк."""
            try:
                if not uid or not updates:
                    await _log(f"[_save] invalid args uid={uid} updates={list(updates) if updates else None}",
                                type_msg="error")
                    ui.notify(lang_dict(err_key, current_lang), type="negative")
                    return False

                ok = await update_table("users", uid, updates)
                if not ok:
                    ui.notify(lang_dict(err_key, current_lang), type="negative")
                    await _log(f"[_save] update_table('users')->False; updates={updates}", type_msg="error")
                    return False

                if merge_state:
                    try:
                        user.update(updates)
                    except Exception as merge_err:
                        await _log(f"[_save] state merge failed: {merge_err!r}", type_msg="warning")

                # внешний колбэк — до перерендера, чтобы новый стейт уже был учтён
                if on_after_ok:
                    try:
                        await on_after_ok()
                    except Exception as cb_err:
                        await _log(f"[_save] on_after_ok failed: {cb_err!r}", type_msg="warning")

                ui.notify(lang_dict(ok_key, current_lang), type="positive")
                await _log(f"[_save] OK: uid={uid} keys={list(updates)}", type_msg="info")

                await _render_section_stub(section)
                return True

            except Exception as e:
                await _log(f"[_save][ОШИБКА] {e!r}", type_msg="error")
                ui.notify(lang_dict(err_key, current_lang), type="negative")
                return False
            
        async def _body(
            *,
            slot: ui.element,
            text_label: str,
            text_label_2: str | None = None,
            value_main: Any,
            value_additional: Any | None = None,
            additional: bool = False,
        ) -> tuple[ui.input, ui.input | None]:
            """Строим части тела диалога и возвращаем ссылки на созданные поля ввода."""
            try:
                with slot:
                    # Заголовок секции поля
                    ui.label(lang_dict(text_label, current_lang)).classes("text-subtitle1")

                    # Основное поле с учётом отсутствующих данных
                    missing_placeholder = lang_dict("profile_value_missing", current_lang)
                    main_value = "" if value_main in (None, "") else str(value_main)
                    input_main = ui.input(value=main_value).props("outlined dense clearable").classes("w-full")
                    if not main_value:
                        input_main.props(f'placeholder="{missing_placeholder}"')

                    additional_input: ui.input | None = None
                    if additional:
                        additional_label = text_label_2 or text_label
                        ui.label(lang_dict(additional_label, current_lang)).classes("text-caption text-grey-6")
                        additional_value = "" if value_additional in (None, "") else str(value_additional)
                        additional_input = ui.input(value=additional_value).props("outlined dense clearable").classes("w-full")
                        if not additional_value:
                            additional_input.props(f'placeholder="{missing_placeholder}"')

                # Обрабатываем переход по Enter между полями
                bind_enter_action(input_main, additional_input if additional else None, close=not additional)
                if additional_input is not None:
                    bind_enter_action(additional_input, close=True)

                return input_main, additional_input
            except Exception as body_error:
                await _log(f"[_body][ОШИБКА] {body_error!r}", type_msg="error")
                raise

        async def show_action_dialog(
            *,
            lang: str,
            actions: list[tuple[str, ActionHandler | None]],
            body_renderer: BodyRenderer,
            persistent: bool = True,
            maximized: bool = False,
            seamless: bool = False,
            danger_keys: set[str] | None = None,
        ) -> None:
            """Карточка по центру; 'X' СВЕРХУ, снаружи карточки (правый верх над ней)."""
            try:
                props: list[str] = []
                if persistent:
                    props.append("persistent")
                if maximized:
                    props.append("maximized")
                if seamless:
                    props.append("seamless")

                dialog = ui.dialog().props(" ".join(props))

                async def _close(_: object | None = None) -> None:
                    try:
                        dialog.close()
                        await _log("[dialog] closed", type_msg="info")
                    except Exception as e:
                        await _log(f"[dialog][close][ОШИБКА] {e!r}", type_msg="error")
                        raise

                def _wrap_action(h: ActionHandler | None) -> ActionHandler:
                    async def _do(arg: object | None = None) -> None:
                        try:
                            if h is not None:
                                await h(arg)
                        finally:
                            await _close()
                    return _do

                with dialog:
                    # Центровка содержимого
                    center = ui.element("div").classes("w-full").style(
                        "position:fixed; inset:0; display:flex; align-items:center; justify-content:center;"
                        # не реагируем на клавиатуру: стабильная высота экрана
                        "min-height:100vh; height:100svh; max-height:100svh;"
                        # безопасные зоны, без var(--kb)
                        "padding: calc(16px + env(safe-area-inset-top,0px)) 16px "
                        "calc(16px + env(safe-area-inset-bottom,0px)) 16px;"
                        "overflow:visible;"
                    )

                    with center:
                        wrapper = ui.element("div").style(
                            "position:relative; display:inline-block; overflow:visible; width:min(560px,92vw);"
                            # если клавиатура огромная — ограничим высоту так, чтобы карточка была видимой
                            "max-height: calc(100% - 32px);"
                        )
                        with wrapper:
                            # Определяем цвет иконки закрытия в зависимости от темы пользователя
                            theme_mode = str(user.get("theme_mode") or "light").lower()
                            close_icon_color = "grey-6" if theme_mode == "dark" else "black"

                            ui.button(icon="close", on_click=_close)\
                            .props(f"flat round dense size=md color={close_icon_color}")\
                            .classes("z-top")\
                            .style("position:absolute; right:12px; bottom: calc(100% + 8px); pointer-events:auto;")

                            with ui.card().classes("w-full q-pa-md gap-3").style(
                                # когда клавиатура есть, внутренности можно прокручивать
                                "max-height: inherit; overflow:auto;"
                            ):
                                body_slot = ui.column().classes("w-full gap-3")
                                await body_renderer(body_slot)
                                with ui.row().classes("w-full justify-end gap-2"):
                                    # Сначала рисуем кнопки с опасными действиями, затем остальные
                                    danger_actions = [
                                        item for item in actions
                                        if danger_keys and item[0] in danger_keys
                                    ]
                                    primary_actions = [
                                        item for item in actions
                                        if not danger_keys or item[0] not in danger_keys
                                    ]

                                    for label_key, handler in [*danger_actions, *primary_actions]:
                                        button = ui.button(
                                            lang_dict(label_key, lang),
                                            on_click=_wrap_action(handler),
                                        )
                                        props_color = "color=primary unelevated flat"
                                        if danger_keys and label_key in danger_keys:
                                            props_color = "color=negative unelevated flat"
                                        button.props(props_color)

                dialog.open()
                await _log("[dialog] opened", type_msg="info")

            except Exception as e:
                await _log(f"[dialog][ОШИБКА] {e!r}", type_msg="error")
                raise

        async def _open_input_dialog(
            *,
            initial_value: Any,
            initial_value_2: Any | None = None,
            label_key: str,
            label_key_2: str | None = None,
            action_key: str,
            updates_factory: Callable[[str, str | None], dict[str, Any]],
            section: str,
            ok_key: str,
            err_key: str,
            additional: bool = False,
            merge_state: bool = True,
            delete_key: str | None = None,
            delete_handler: Callable[[], Awaitable[None]] | None = None,
            post_merge: Callable[[], Awaitable[None]] | None = None,
        ) -> None:
            """Унифицируем логику одиночного диалога ввода."""
            try:
                input_main: ui.input | None = None
                input_additional: ui.input | None = None

                async def _render_body(slot: ui.element) -> None:
                    nonlocal input_main, input_additional
                    try:
                        input_main, input_additional = await _body(
                            slot=slot,
                            text_label=label_key,
                            text_label_2=label_key_2,
                            value_additional=initial_value_2,
                            additional=additional,
                            value_main=initial_value,
                        )
                    except Exception as body_error:
                        await _log(
                            f"[profile_menu][field_dialog][body][ОШИБКА] {body_error!r}",
                            type_msg="error",
                        )
                        raise

                async def _handle_save(_: Any | None = None) -> None:
                    try:
                        new_value: str = (
                            input_main.value if input_main else ""
                        ).strip()
                        new_value_2: str | None = (
                            (input_additional.value if input_additional else "").strip()
                            if input_additional is not None
                            else None
                        )

                        try:
                            updates = updates_factory(new_value, new_value_2)
                        except ValueError as validation_error:
                            error_key = (
                                str(validation_error)
                                if validation_error.args
                                else err_key
                            )
                            ui.notify(
                                lang_dict(error_key, current_lang),
                                type="warning",
                            )
                            await _log(
                                f"[profile_menu][field_dialog][validation] {validation_error}",
                                type_msg="warning",
                            )
                            return

                        await _save(
                            updates=updates,
                            section=section,
                            ok_key=ok_key,
                            err_key=err_key,
                            on_after_ok=post_merge,
                            merge_state=merge_state,
                        )
                    except Exception as save_error:
                        await _log(
                            f"[profile_menu][field_dialog][save][ОШИБКА] {save_error!r}",
                            type_msg="error",
                        )
                        ui.notify(
                            lang_dict(err_key, current_lang),
                            type="negative",
                        )
                        raise

                actions_spec: list[tuple[str, ActionHandler | None]] = [
                    (action_key, _handle_save)
                ]

                if delete_key and delete_handler is not None:
                    async def _handle_delete(_: Any | None = None) -> None:
                        await delete_handler()

                    actions_spec.append((delete_key, _handle_delete))

                danger_keys = {delete_key} if delete_key else None

                await show_action_dialog(
                    lang=current_lang,
                    actions=actions_spec,
                    body_renderer=_render_body,
                    persistent=True,
                    maximized=False,
                    danger_keys=danger_keys,
                )
            except Exception as dialog_error:
                await _log(
                    f"[profile_menu][field_dialog][ОШИБКА] {dialog_error!r}",
                    type_msg="error",
                )
                raise


        async def _render_deleted_state() -> None:
            """Показываем заглушку после удаления профиля."""
            try:
                root.clear()
                with root:
                    with ui.column().classes(
                        "w-full gap-3 q-pa-xl items-center justify-center text-center"
                    ):
                        ui.icon("delete_forever").props("size=72px color=negative")
                        ui.label(
                            lang_dict("profile_delete_success", current_lang),
                        ).classes("text-h6")
                await _log(
                    "[profile_menu] показана заглушка после удаления",
                    type_msg="info",
                )
            except Exception as deleted_error:
                await _log(
                    f"[profile_menu][deleted_state][ОШИБКА] {deleted_error!r}",
                    type_msg="error",
                )
                raise

        async def _confirm_delete() -> None:
            """Диалог подтверждения удаления профиля."""
            nonlocal user_deleted
            try:
                if not uid:
                    ui.notify(
                        lang_dict("profile_delete_error", current_lang),
                        type="negative",
                    )
                    tabs.set_value("profile_list")
                    return

                dialog = ui.dialog().props("persistent")

                async def _cancel(_: Any | None = None) -> None:
                    try:
                        await _log(
                            "[profile_menu] удаление отменено пользователем",
                            type_msg="info",
                        )
                        dialog.close()
                        await _go_back_to_profile()
                    except Exception as cancel_error:
                        await _log(
                            f"[profile_menu][delete_cancel][ОШИБКА] {cancel_error!r}",
                            type_msg="error",
                        )
                        raise

                async def _perform(_: Any | None = None) -> None:
                    nonlocal user_deleted
                    try:
                        if not await delete_user(uid):
                            ui.notify(
                                lang_dict("profile_delete_error", current_lang),
                                type="negative",
                            )
                            return
                        user_deleted = True
                        await _log(
                            f"[profile_menu] профиль удален user_id={uid}",
                            type_msg="info",
                        )
                        dialog.close()
                        ui.notify(
                            lang_dict("profile_delete_success", current_lang),
                            type="positive",
                        )
                        await _go_back_to_profile()
                    except Exception as delete_error:
                        await _log(
                            f"[profile_menu][delete][ОШИБКА] {delete_error!r}",
                            type_msg="error",
                        )
                        raise

                with dialog:
                    with ui.card().classes("w-full gap-3 q-pa-md"):
                        ui.label(
                            lang_dict("profile_delete_title", current_lang),
                        ).classes("text-h6")
                        ui.label(
                            lang_dict("profile_delete_confirm", current_lang),
                        ).classes("text-body2")
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button(
                                lang_dict(
                                    "profile_delete_confirm_action",
                                    current_lang,
                                ),
                                on_click=_perform,
                            ).props("color=negative flat")
                            ui.button(
                                lang_dict("profile_delete_cancel", current_lang),
                                on_click=_cancel,
                            ).props("color=primary flat")

                dialog.open()
            except Exception as confirm_error:
                await _log(
                    f"[profile_menu][delete_dialog][ОШИБКА] {confirm_error!r}",
                    type_msg="error",
                )
                raise

        async def _handle_tab_change(event: ui.events.ValueChangeEventArguments) -> None:
            """Реакция на переключение вкладок профиля."""
            global active_tab
            try:
                value = (event.value or "").strip()
                active_tab = value
                await _log(
                    f"[profile_menu][tab_change] -> {value}",
                    type_msg="info",
                )

                if user_deleted:
                    if value != "profile_list":
                        ui.notify(
                            lang_dict("profile_delete_success", current_lang),
                            type="warning",
                        )
                        tabs.set_value("profile_list")
                        return
                    await _render(current_lang)
                    return

                if value == "profile_list":
                    await _render(current_lang)
                    return

                if value in SECTION_TITLE_KEYS:
                    await _render_section_stub(value)
                    return

                if value == "role_switch":
                    if await _switch_role():
                        tabs.set_value("profile_list")
                    return

                if value == "delete":
                    await _confirm_delete()
                    return
            except Exception as tab_error:
                await _log(
                    f"[profile_menu][tab_change][ОШИБКА] {tab_error!r}",
                    type_msg="error",
                )
                ui.notify(
                    lang_dict("profile_tab_change_error", current_lang),
                    type="negative",
                )
                raise

        tabs.on_value_change(_handle_tab_change)

        # === Центральная функция повторной отрисовки профиля ===
        async def _render(lang: str) -> None:
            """Полностью пересобирает раздел профиля."""
            nonlocal current_lang, current_role, registration_dialog, reg_container, user_deleted
            current_lang = lang
            current_role = (user.get("role") or "passenger").lower()

            root.clear()
            tabs.clear()

            available_tabs = ["profile_list", "personal_data"]
            if current_role == "driver":
                available_tabs.extend(["car_data", "balance"])
            available_tabs.extend(["settings", "support", "role_switch", "delete"])

            with tabs:
                for tab_name in available_tabs:
                    ui.tab("").props(f'name="{tab_name}"')

            if user_deleted:
                await _render_deleted_state()
                return

            rating_key = (
                "profile_rating_passenger"
                if current_role == "passenger"
                else "profile_rating_driver"
            )
            rating_value = _format_rating(
                user.get("rating_passenger")
                if current_role == "passenger"
                else user.get("rating_driver")
            )

            if avatar_url:
                await _log(
                    f"[profile_menu] аватар подготовлен (len={len(avatar_url)})",
                    type_msg="info",
                )
            else:
                await _log(
                    "[profile_menu] аватар не найден, используется иконка",
                    type_msg="warning",
                )

            async def _panel_header(icon_name: str, text_key: str, user_lang: str) -> None:
                """Универсальная шапка панели: [icon] label ......................... [>]."""
                exclude_sep = {"settings", "setting", "delete_forever"}
                without_chevron = {"delete_forever", "support_agent", "swap_horiz"}
                with ui.column().classes("w-full"):
                    with ui.row().classes(
                        "w-full items-center no-wrap q-px-none q-py-none"
                    ):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon(icon_name).props("size=22px")
                            ui.label(lang_dict(text_key, user_lang)).classes(
                                "text-body1"
                            )
                        ui.space()
                        if icon_name not in without_chevron:
                            ui.icon("chevron_right").props("size=24px")

                    if icon_name in exclude_sep:
                        return
                    ui.separator().classes("w-full q-my-none")

            role_switch_key = (
                "profile_role_switch_to_driver"
                if current_role == "passenger"
                else "profile_role_switch_to_passenger"
            )

            with root:
                header_card = ui.card().classes("w-full  q-pa-none gap-4")
                with header_card:
                    with ui.row().classes("items-center gap-4 no-wrap"):
                        avatar = ui.avatar().props("size=84").classes(
                            "profile-avatar"
                        )
                        if avatar_url:
                            avatar.props("color=transparent text-color=transparent")
                            avatar.style(
                                "background-color: transparent; border: none; padding: 0; box-shadow: none;"
                            )
                            with avatar:
                                ui.image(avatar_url).classes(
                                    "w-full h-full"
                                ).style(
                                    "object-fit: cover; border-radius: inherit;"
                                )
                        else:
                            avatar.props("icon=person color=primary text-color=white")

                        with ui.column().classes("gap-1"):
                            ui.label(
                                user.get("first_name")
                                or lang_dict("unknown_user", current_lang)
                            ).props("text-color=primary").classes("text-h6")
                            with ui.row().classes(
                                "items-center gap-2 text-body1 text-primary"
                            ):
                                ui.icon("star").props("color=primary size=22")
                                ui.label(
                                    lang_dict(
                                        rating_key,
                                        current_lang,
                                        value=rating_value,
                                    )
                                )

                with ui.card().classes("w-full gap-0 q-px-none q-py-none q-my-none"):
                    with ui.tab_panel("personal_data").classes("w-full") as personal_panel:
                        await _panel_header(
                            "person",
                            "profile_personal_title",
                            current_lang,
                        )
                    _attach_panel_click(personal_panel, "personal_data")

                    if current_role == "driver":
                        with ui.tab_panel("car_data").classes("w-full") as car_panel:
                            await _panel_header(
                                "directions_car",
                                "profile_driver_vehicle_title",
                                current_lang,
                            )
                        _attach_panel_click(car_panel, "car_data")

                        with ui.tab_panel("balance").classes("w-full") as balance_panel:
                            await _panel_header(
                                "account_balance_wallet",
                                "profile_balance_title",
                                current_lang,
                            )
                        _attach_panel_click(balance_panel, "balance")

                    with ui.tab_panel("settings").classes("w-full") as settings_panel:
                        await _panel_header(
                            "settings",
                            "profile_settings_title",
                            current_lang,
                        )
                    _attach_panel_click(settings_panel, "settings")

                registration_dialog = ui.dialog().props("persistent maximized")
                with registration_dialog:
                    reg_container = ui.column().classes(
                        "w-full h-full gap-4 q-pa-md overflow-auto"
                    )

                with ui.card().classes("w-full gap-0 q-px-none q-py-none q-my-none"):
                    with ui.tab_panel("role_switch").classes(
                        "w-full q-pa-none"
                    ) as role_panel:
                        await _panel_header(
                            "swap_horiz",
                            role_switch_key,
                            current_lang,
                        )
                    _attach_panel_click(role_panel, "role_switch")

                    with ui.tab_panel("support").classes(
                        "w-full q-pa-none"
                    ) as support_panel:
                        await _panel_header(
                            "support_agent",
                            "profile_support_title",
                            current_lang,
                        )
                    _attach_panel_click(support_panel, "support")

                    with ui.tab_panel("delete").classes("w-full q-pa-none") as delete_panel:
                        await _panel_header(
                            "delete_forever",
                            "profile_delete_title",
                            current_lang,
                        )
                    _attach_panel_click(delete_panel, "delete")

        await _render(current_lang)

        await _log("[profile_menu] render complete", type_msg="info")
        return None

    except Exception as e:
        await _log(f"[profile_menu][ОШИБКА] {e!r}", type_msg="error")
        raise
