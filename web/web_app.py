from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from starlette.middleware.sessions import SessionMiddleware
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from nicegui import ui, app, storage
from web.web_decorators import require_twa, with_theme_toggle
from web.web_start_reg_form import start_reg_form_ui
from web.web_utilits import get_user_data_uid_lang, _safe_js
from web.web_order_menu import render_order_tab
from web.splash.splash_animation import splash_screen
from web.web_profile_menu import render_profile_menu_tab
from web.web_main_menu import render_main_map

from log.log import log_info
from config.config_utils import lang_dict

NAV_TABS   = ('main', 'order', 'profile')
PANEL_TABS = ('main', 'order', 'profile', 'start_reg_form_with_choice_role', 'start_reg_form')
    
_last_nav_ts: float = 0.0

# ============================================================================
# FastAPI + NiceGUI Setup
# ============================================================================

# 1) Базовый FastAPI
fastapi_app = FastAPI()


class ClientLogPayload(BaseModel):
    """Модель тела запроса, отправляемого фронтендом."""

    level: str | None = Field(default=None)
    message: str | None = Field(default=None)
    stack: str | None = Field(default=None)
    source: str | None = Field(default=None)
    line: int | None = Field(default=None)
    column: int | None = Field(default=None)
    url: str | None = Field(default=None)
    user_agent: str | None = Field(default=None, alias="userAgent")
    timestamp: float | None = Field(default=None)
    client_id: str | int | None = Field(default=None, alias="clientId")
    user_id: int | str | None = Field(default=None, alias="userId")
    extra: dict[str, Any] | None = Field(default=None)

    class Config:
        populate_by_name = True

# 2) Middleware
fastapi_app.add_middleware(storage.RequestTrackingMiddleware)
fastapi_app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv('STORAGE_SECRET', 'change-me-please')
)

# 3) Прикрепляем NiceGUI к FastAPI
ui.run_with(
    fastapi_app, 
    storage_secret=os.getenv('STORAGE_SECRET', 'change-me-please')
)


@app.post('/api/client_log')
async def client_log(request: Request) -> dict[str, str]:
    """Принимаем сообщения об ошибках с фронтенда и зеркалим их в лог."""
    try:
        # ------------------------------------------------------------------
        # Читаем исходное тело запроса и пытаемся преобразовать к словарю.
        # ------------------------------------------------------------------
        raw_body: bytes = b""
        try:
            raw_body = await request.body()
        except Exception as read_error:
            await log_info(
                "[client_log] не удалось прочитать тело запроса",
                type_msg="warning",
                reason=str(read_error),
            )

        payload_source: dict[str, Any]
        if raw_body:
            try:
                parsed_body = json.loads(raw_body)
                if isinstance(parsed_body, dict):
                    payload_source = parsed_body
                else:
                    await log_info(
                        "[client_log] тело запроса имеет неподдерживаемый формат",
                        type_msg="warning",
                        incoming_value=parsed_body,
                    )
                    payload_source = {}
            except json.JSONDecodeError as decode_error:
                raw_fragment = raw_body.decode('utf-8', errors='replace')[:2000]
                await log_info(
                    "[client_log] не удалось разобрать JSON из тела запроса",
                    type_msg="warning",
                    reason=str(decode_error),
                    raw_fragment=raw_fragment,
                )
                payload_source = {}
        else:
            payload_source = {}

        payload_model: ClientLogPayload | None = None
        if payload_source:
            try:
                payload_model = ClientLogPayload.model_validate(payload_source)
            except ValidationError as validation_error:
                errors_list = validation_error.errors()
                truncated_payload = json.dumps(payload_source, ensure_ascii=False)[:1000]
                await log_info(
                    "[client_log] данные не прошли валидацию модели; см. ошибки и сокращённый payload",
                    type_msg="warning",
                    errors=errors_list,
                    raw_payload=truncated_payload,
                )
        if payload_model is None:
            payload_model = ClientLogPayload()

        # ------------------------------------------------------------------
        # Достаём значения из модели либо исходного словаря.
        # ------------------------------------------------------------------
        client_host: str | None = request.client.host if request.client else None

        def _value(attr: str, alias: str | None = None) -> Any:
            if getattr(payload_model, attr) is not None:
                return getattr(payload_model, attr)
            if alias is not None and alias in payload_source:
                return payload_source.get(alias)
            return payload_source.get(attr)

        level_raw: str = str(_value('level')).lower() if _value('level') else 'error'
        if 'error' in level_raw:
            type_msg = 'error'
        elif 'warn' in level_raw:
            type_msg = 'warning'
        else:
            type_msg = 'info'

        line_value = _value('line')
        column_value = _value('column')
        user_id_value = _value('user_id', 'userId')
        client_id_value = _value('client_id', 'clientId')

        details: list[str] = [
            f"уровень={level_raw}",
            f"сообщение={_value('message') or '—'}",
            f"источник={_value('source') or 'не указан'}",
            f"позиция={line_value}:{column_value}" if line_value is not None else 'позиция=нет данных',
            f"url={_value('url') or 'не указано'}",
            f"ip={client_host or 'не определён'}",
        ]
        if user_id_value is not None:
            details.append(f"user_id={user_id_value}")
        if client_id_value:
            details.append(f"client_id={client_id_value}")

        await log_info(
            f"[client_log] {'; '.join(details)}",
            type_msg=type_msg,
            stack=_value('stack'),
            user_agent=_value('user_agent', 'userAgent'),
            timestamp=_value('timestamp'),
            extra_payload=_value('extra'),
        )

        return {"status": "ok"}
    except Exception as err:
        await log_info(
            f"[client_log][ОШИБКА] {err!r}",
            type_msg="error",
        )
        raise HTTPException(status_code=500, detail="Не удалось сохранить клиентский лог")


# ============================================================================
# Сервер Uvicorn
# ============================================================================

async def start_server(host: str = '0.0.0.0', port: int | None = None):
    """Запуск Uvicorn сервера как фоновой задачи"""
    try:
        port = port or int(os.getenv('NG_PORT', '8080'))
        await log_info(
            f"[server] запуск: host={host}, port={port}", 
            type_msg="info"
        )
        
        import uvicorn
        
        config = uvicorn.Config(
            app=fastapi_app,
            host=host,
            port=port,
            log_level='info',
        )
        server = uvicorn.Server(config)

        # Запускаем сервер как фоновую задачу
        task = asyncio.create_task(server.serve())

        # Ждём старта сокета
        while not getattr(server, 'started', False):
            await asyncio.sleep(0.05)

        await log_info(
            f"[server] uvicorn стартовал на :{port}", 
            type_msg="info"
        )
        return server, task
        
    except Exception as e:
        await log_info(f"[server][ОШИБКА] {e!r}", type_msg="error")
        raise


# ============================================================================
# Главная страница приложения
# ============================================================================

@ui.page('/main_app')
@require_twa                 # Инициализация TWA и загрузка темы
@splash_screen(
    svg_path=None,                            # Ваш SVG файл
    duration=3000,                            # Минимальное время показа
    fade_in=300,                              # Время появления
    fade_out=500,                             # Время исчезновения
    auto_hide=True                            # Автоматически скрыть
)
@with_theme_toggle(False)          # Добавление переключателя темы
async def main_app():
    """
    Главная страница Mini App с навигацией по вкладкам.
    
    Декораторы применяются в порядке:
    1. @with_svg_splash_img - показывает splash screen
    2. @require_twa - инициализирует Telegram WebApp и загружает тему
    3. @with_theme_toggle - добавляет переключатель темы
    """
    try:
        async def ensure_user_role_or_start_registration(
            uid: int,
            user_lang: str,
            user_data: dict[str, Any] | None,
        ) -> bool:
            """Если роль отсутствует/unknown — переключаемся на панель регистрации и прячем футер."""
            try:
                role_raw = (user_data or {}).get('role')
                role = str(role_raw).strip().lower() if role_raw is not None else ""

                if not role or role in {'unknown', 'none', 'null', 'undefined'}:
                    await log_info("[role_check] роль не определена → открываем панель регистрации", type_msg="info", uid=uid)
                    # приоритет панели и навигации
                    app.storage.user['panel'] = 'start_reg_form_with_choice_role'
                    app.storage.user['nav'] = 'main'
                    # аккуратно обновим URL (без перезагрузки)
                    await _safe_js("history.replaceState(null,'','/main_app?tab=start_reg_form_with_choice_role')", timeout=1.5)
                    return True
                else:
                    await log_info(f"[role_check] роль пользователя определена: {role}", type_msg="info", uid=uid)
                    return False

            except Exception as e:
                await log_info("[role_check][ОШИБКА] не удалось обработать роль пользователя", type_msg="error", uid=uid, reason=str(e))
                ui.notify(lang_dict("map_notify_unexpected_error", user_lang), type="warning")
                return False
            

        await log_info("[page:/main_app] рендер начат", type_msg="info")

        try:
            # Получение данных пользователя
            uid, user_lang, user_data = await get_user_data_uid_lang()
            user_lang = user_lang or 'en'

            # 1) Проверка роли
            started_reg = await ensure_user_role_or_start_registration(uid, user_lang, user_data)

            # 2) Определяем стартовую панель (учитываем приоритет регистрации)
            try:
                start_tab = await ui.run_javascript(
                    "new URLSearchParams(location.search).get('tab') || null",
                    timeout=3.0,
                )
            except Exception:
                start_tab = None

            if started_reg:
                panel = 'start_reg_form_with_choice_role'
            else:
                panel = (start_tab if start_tab in PANEL_TABS else app.storage.user.get('panel') or 'main')

            nav = (panel if panel in NAV_TABS else app.storage.user.get('nav') or 'main')
            app.storage.user['panel'] = panel
            app.storage.user['nav']   = nav

            # ====================================================================
            # UI: Контейнер панелей
            # ====================================================================

            with ui.column().classes('w-full q-pa-none q-ma-none main-app-content'):
                with ui.tab_panels() \
                        .bind_value(app.storage.user, 'panel') \
                        .props('animated keep-alive transition-prev=fade transition-next=fade') \
                        .classes('w-full') as panels:
                    app.storage.client['tab_panels'] = panels

                    # Панель: Главная
                    with ui.tab_panel('main').classes('w-full q-pa-none q-ma-none flex flex-col'):
                        # Создаём статический контейнер карты, чтобы не терять DOM при фоновых переходах
                        map_wrapper_id = "main_map_wrapper"
                        map_container_id = "main_map_canvas"
                        container_classes = "w-full h-full taxibot-map-canvas"
                        map_wrapper = ui.element('div').classes(
                            "w-full q-pa-none q-ma-none flex-1 relative overflow-hidden"
                        )
                        map_wrapper.props(f"id={map_wrapper_id}")
                        map_wrapper.style(
                            "min-height: calc(var(--main-app-viewport, 100vh) - var(--main-footer-height, 0px));"
                            "height: calc(var(--main-app-viewport, 100vh) - var(--main-footer-height, 0px));"
                            "width: 100%;"
                        )
                        with map_wrapper:
                            ui.element('div').classes(container_classes).props(f"id={map_container_id}")
                        main_map_state: dict[str, Any] = {
                            "rendered": False,
                        }

                        async def _render_main_tab_map(force: bool = False) -> None:
                            """Отрисовывает карту только при наличии DOM вкладки 'main'."""
                            if main_map_state["rendered"] and not force:
                                return
                            try:
                                await render_main_map(
                                    uid,
                                    user_lang,
                                    user_data,
                                    wrapper_id=map_wrapper_id,
                                    container_id=map_container_id,
                                )
                                main_map_state["rendered"] = True
                            except Exception as render_error:  # noqa: BLE001
                                main_map_state["rendered"] = False
                                await log_info(
                                    "[main_tab] ошибка повторного рендера карты",
                                    type_msg="warning",
                                    reason=str(render_error),
                                )

                        app.storage.client["main_map_render_cb"] = _render_main_tab_map

                        if panel == 'main':
                            await _render_main_tab_map(force=True)
                        #print("Main map rendering is currently disabled.")

                    # Панель: Поездки
                    with ui.tab_panel('order').classes('w-full q-pa-none q-ma-none q-mt-xl flex flex-col'):
                        #await render_order_tab(uid, user_lang, user_data)
                        print("Order tab rendering is currently disabled.")

                    # Панель: Профиль
                    with ui.tab_panel('profile').classes('w-full q-pa-none q-ma-none q-mt-xl flex flex-col'):
                        await render_profile_menu_tab(uid, user_lang, user_data)

                    # Панель: Регистрация (скрытая, без кнопки в футере)
                    with ui.tab_panel('start_reg_form_with_choice_role').classes('w-full q-pa-none q-ma-none q-mt-xl flex flex-col'):
                        await start_reg_form_ui(uid, user_lang, user_data, choice_role=True)

                    with ui.tab_panel('start_reg_form').classes('w-full q-pa-none q-ma-none q-mt-xl flex flex-col'):
                        await start_reg_form_ui(uid, user_lang, user_data, choice_role=False)

            # ====================================================================
            # UI: Футер с навигацией
            # ====================================================================
            
            if not started_reg:
                with ui.footer().props('bordered').classes('app-footer no-shadow'):
                
                    tabs = (
                        ui.tabs()
                        .bind_value(app.storage.user, 'nav')
                        .props('dense no-caps align=justify narrow-indicator '
                            'active-color=primary indicator-color=primary')
                        .classes('w-full')
                    )
                    app.storage.client['nav_tabs'] = tabs
                    
                    with tabs:
                        ui.tab(
                            'main', 
                            label=lang_dict('footer_main', user_lang), 
                            icon='home'
                        ).props('stack')
                        
                        ui.tab(
                            'order', 
                            label=lang_dict('footer_order', user_lang), 
                            icon='local_taxi'
                        ).props('stack')
                        
                        ui.tab(
                            'profile', 
                            label=lang_dict('footer_profile', user_lang), 
                            icon='person'
                        ).props('stack')
                    
                    # Обработчик изменения вкладки
                    async def _on_nav_change(e):
                        global _last_nav_ts
                        now = time.monotonic()
                        if now - _last_nav_ts < 0.20:  # 200 мс
                            return
                        _last_nav_ts = now
                        try:
                            panels.set_value(e.value)

                            if e.value == "main":
                                main_map_cb = app.storage.client.get("main_map_render_cb")
                                if callable(main_map_cb):
                                    try:
                                        await main_map_cb()
                                    except Exception as map_cb_error:  # noqa: BLE001
                                        await log_info(
                                            "[nav.change] не удалось отрисовать карту при переходе на main",
                                            type_msg="warning",
                                            reason=str(map_cb_error),
                                        )

                            profile_sync = app.storage.client.get("profile_back_sync")
                            if callable(profile_sync):
                                if e.value == "profile":
                                    current_section = app.storage.client.get("profile_active_tab") or "profile_list"
                                    await profile_sync(current_section)
                                else:
                                    await profile_sync("profile_list")

                            order_reset = app.storage.client.get("order_back_reset")
                            if callable(order_reset):
                                await order_reset()

                            # Обновляем URL безопасно, без падения при таймауте.
                            code = f"history.replaceState(null, '', '/main_app?tab={e.value}')"
                            # Вариант А: сразу, но с try/except и большим timeout
                            ok = await _safe_js(code, timeout=3.0)

                            # Вариант B (доп.): если не успели — попробуем «следующим тиком»
                            if not ok:
                                ui.timer(0.0, lambda: ui.run_javascript(code), once=True)

                        except Exception as err:
                            await log_info(f"[nav.change][ОШИБКА] {err!r}", type_msg="error")
                
                tabs.on_value_change(_on_nav_change)

            # --------------------------------------------------------------------
            # JS: синхронизация высоты футера и доступной области (безопасная зона)
            # --------------------------------------------------------------------
            try:
                await ui.run_javascript(
                    """
                                    (function(){
                                        try {
                                            const doc = document.documentElement;
                                            const flagKey = '__main_app_vh_bound';
                                            if (doc[flagKey]) { return true; }
                                            const telegram = window.Telegram?.WebApp;

                                            // Управляем видимостью футера при открытии экранной клавиатуры
                                            const state = {
                                                footerHeight: 0,
                                                footerHidden: false,
                                                keyboardActive: false,
                                                viewportKeyboard: false,
                                                restoreTimer: null,
                                            };

                                            // Список типов input, которые раскрывают клавиатуру
                                            const textInputTypes = new Set([
                                                'text', 'search', 'email', 'password', 'tel', 'url', 'number',
                                                'datetime-local', 'date', 'time', 'month', 'week'
                                            ]);

                                            const getFooter = () => document.querySelector('.app-footer');

                                            const applyFooterHeight = (value) => {
                                                state.footerHeight = value;
                                                doc.style.setProperty('--main-footer-height', `${value}px`);
                                            };

                                            const hideFooter = () => {
                                                if (state.footerHidden) { return; }
                                                const footer = getFooter();
                                                if (!footer) { return; }
                                                const rect = footer.getBoundingClientRect();
                                                if (rect.height > 0) {
                                                    state.footerHeight = rect.height;
                                                }
                                                footer.classList.add('app-footer--hidden');
                                                doc.style.setProperty('--main-footer-height', '0px');
                                                state.footerHidden = true;
                                            };

                                            const showFooter = () => {
                                                if (!state.footerHidden) { return; }
                                                const footer = getFooter();
                                                if (!footer) { return; }
                                                footer.classList.remove('app-footer--hidden');
                                                doc.style.setProperty('--main-footer-height', `${state.footerHeight}px`);
                                                state.footerHidden = false;
                                            };

                                            const isEditable = (element) => {
                                                if (!element) { return false; }
                                                if (element.isContentEditable) { return true; }
                                                const tag = element.tagName?.toLowerCase();
                                                if (tag === 'textarea') { return true; }
                                                if (tag !== 'input') { return false; }
                                                const type = element.type?.toLowerCase() || 'text';
                                                return textInputTypes.has(type);
                                            };

                                            // Пересчитываем высоты и определяем состояние клавиатуры
                                            const KEYBOARD_THRESHOLD = 40;

                                            const updateViewport = () => {
                                                const currentHeight = telegram?.viewportHeight || window.innerHeight;
                                                const stableHeight = telegram?.viewportStableHeight || currentHeight;
                                                const heightGap = Math.max(0, stableHeight - currentHeight);
                                                const keyboardLikely = heightGap > KEYBOARD_THRESHOLD;
                                                state.viewportKeyboard = keyboardLikely;
                                                const baseHeight = keyboardLikely ? currentHeight : stableHeight;
                                                const effectiveGap = keyboardLikely ? Math.max(0, Math.round(heightGap)) : 0;
                                                doc.style.setProperty('--main-app-viewport', `${baseHeight}px`);
                                                doc.style.setProperty('--keyboard-gap', `${effectiveGap}px`);
                                                const footer = getFooter();
                                                if (footer && !state.footerHidden) {
                                                    applyFooterHeight(footer.getBoundingClientRect().height);
                                                }
                                                if (keyboardLikely) {
                                                    hideFooter();
                                                } else if (!state.keyboardActive) {
                                                    showFooter();
                                                }
                                            };

                                            const scheduleRestore = () => {
                                                if (state.restoreTimer) {
                                                    window.clearTimeout(state.restoreTimer);
                                                }
                                                state.restoreTimer = window.setTimeout(() => {
                                                    if (!state.keyboardActive && !state.viewportKeyboard) {
                                                        showFooter();
                                                    }
                                                }, 180);
                                            };

                                            document.addEventListener('focusin', (event) => {
                                                if (isEditable(event.target)) {
                                                    state.keyboardActive = true;
                                                    hideFooter();
                                                }
                                            }, true);

                                            document.addEventListener('focusout', (event) => {
                                                if (isEditable(event.target)) {
                                                    state.keyboardActive = false;
                                                    scheduleRestore();
                                                }
                                            }, true);

                                            window.addEventListener('resize', updateViewport);
                                            telegram?.onEvent?.('viewportChanged', updateViewport);

                                            doc[flagKey] = true;
                                            updateViewport();
                                            return true;
                                        } catch (bindError) {
                                            console.warn('viewport/keyboard binding failed', bindError);
                                            return false;
                                        }
                                    })();
                    """,
                    timeout=3.0,
                )
            except Exception as js_error:
                await log_info(
                    f"[page:/main_app][viewport][ОШИБКА] {js_error!r}",
                    type_msg="warning",
                )

            # Регистрируем перехват ошибок фронтенда и передачу на сервер
            client_log_user_json = json.dumps(uid)
            try:
                await ui.run_javascript(
                    f"""
                    (function(){{
                    if (window.__clientLogBound) {{ return; }}
                    window.__clientLogBound = true;
                    const endpoint = '/api/client_log';
                    const userId = {client_log_user_json};
                    
                    const sendLog = (payload) => {{
                        try {{
                        const base = {{
                            url: window.location.href,
                            userAgent: navigator.userAgent,
                            timestamp: Date.now(),
                            userId: userId,
                            clientId: window.Telegram?.WebApp?.initDataUnsafe?.user?.id ?? null,
                        }};
                        const bodyObj = Object.assign(base, payload || {{}});
                        const body = JSON.stringify(bodyObj);
                        if (navigator.sendBeacon) {{
                            const blob = new Blob([body], {{ type: 'application/json' }});
                            navigator.sendBeacon(endpoint, blob);
                            return true;
                        }}
                        fetch(endpoint, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body,
                            keepalive: true,
                        }}).catch(() => {{}});
                        return true;
                        }} catch (transportError) {{
                        console.warn('client_log send failed', transportError);
                        return false;
                        }}
                    }};
                    
                    sendLog({{ level: 'info', message: 'client_log_ready' }});
                    
                    window.addEventListener('error', (event) => {{
                        if (!event) {{ return; }}
                        sendLog({{
                        level: 'error',
                        message: event.message || null,
                        source: event.filename || null,
                        line: event.lineno || null,
                        column: event.colno || null,
                        stack: event.error?.stack || null,
                        }});
                    }}, true);
                    
                    window.addEventListener('unhandledrejection', (event) => {{
                        if (!event) {{ return; }}
                        let message = 'Unhandled rejection';
                        let stack = null;
                        if (event.reason) {{
                        if (typeof event.reason === 'string') {{
                            message = event.reason;
                        }} else if (event.reason && typeof event.reason === 'object') {{
                            message = event.reason.message || message;
                            stack = event.reason.stack || null;
                        }}
                        }}
                        sendLog({{
                        level: 'error',
                        message,
                        stack,
                        }});
                    }});
                    }})();
                    """,
                    timeout=3.0,
                )
            except Exception as js_error:
                await log_info(
                    f"[page:/main_app][client_log_js][ОШИБКА] {js_error!r}",
                    type_msg="warning",
                )
            # ====================================================================
            # Синхронизация состояния для прямых ссылок
            # ====================================================================
            
            # Если пришли по ссылке на start_reg_form, подсветить "Главная" в футере
            if panel == 'start_reg_form' and app.storage.user.get('nav') != 'main':
                app.storage.user['nav'] = 'main'

            await log_info("[page:/main_app] рендер завершён", type_msg="info")

        except Exception as e:
            await log_info(f"[page:/main_app][ОШИБКА] {e!r}", type_msg="error")
            raise
            
    except Exception as e:
        await log_info(
            f"[page:/main_app][ОШИБКА] {e!r}", 
            type_msg="error"
        )
        raise


# ============================================================================
# Страница регистрации (отдельная)
# ============================================================================
@ui.page('/start_reg_form')
@require_twa
@splash_screen(
    svg_path=None,                            # Ваш SVG файл
    duration=3000,                            # Минимальное время показа
    fade_in=300,                              # Время появления
    fade_out=500,                             # Время исчезновения
    auto_hide=True                            # Автоматически скрыть
)
@with_theme_toggle(True)           # Добавление переключателя темы
async def reg_form_page():  
    """Страница регистрации пользователя."""
    try:
        await log_info("[page:/start_reg_form] рендер начат", type_msg="info")

        # Получение данных пользователя
        uid, user_lang, user_data = await get_user_data_uid_lang()
        user_lang = user_lang or 'en'

        # UI: Форма регистрации
        await start_reg_form_ui(uid, user_lang, user_data, choice_role=True)

        await log_info("[page:/start_reg_form] рендер завершён", type_msg="info")

    except Exception as e:
        await log_info(
            f"[page:/start_reg_form][ОШИБКА] {e!r}", 
            type_msg="error"
        )
        raise