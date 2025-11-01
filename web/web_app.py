from __future__ import annotations

import asyncio
import os
import time
from starlette.middleware.sessions import SessionMiddleware
from fastapi import FastAPI
from nicegui import ui, app, storage
from web.web_decorators import require_twa, with_theme_toggle
from web.web_start_reg_form import start_reg_form_ui
from web.web_utilits import get_user_data_uid_lang, _safe_js
from web.splash.splash_animation import splash_screen
from web.web_profile_menu import profile_menu

from log.log import log_info
from config.config_utils import lang_dict

NAV_TABS   = ('main', 'trips', 'profile')
PANEL_TABS = ('main', 'trips', 'profile', 'start_reg_form')

_last_nav_ts: float = 0.0

# ============================================================================
# FastAPI + NiceGUI Setup
# ============================================================================

# 1) Базовый FastAPI
fastapi_app = FastAPI()

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
        await log_info("[page:/main_app] рендер начат", type_msg="info")

        # Получение данных пользователя
        uid, user_lang, user_data = await get_user_data_uid_lang()
        user_lang = user_lang or 'en'

        # Определение начальной вкладки из URL (без перезагрузки страницы)
        try:
            start_tab = await ui.run_javascript(
                "new URLSearchParams(location.search).get('tab') || null",
                timeout=3.0,
            )
        except TimeoutError as timeout_error:
            await log_info(
                f"[page:/main_app][start_tab][ОШИБКА] {timeout_error!r}",
                type_msg="warning",
            )
            start_tab = None
        except Exception as js_error:
            await log_info(
                f"[page:/main_app][start_tab][ОШИБКА] {js_error!r}",
                type_msg="error",
            )
            start_tab = None

        # Реактивное состояние: активная панель и активная кнопка футера
        panel = (
            start_tab if start_tab in PANEL_TABS
            else app.storage.user.get('panel') or 'main'
        )
        nav = (
            panel if panel in NAV_TABS
            else app.storage.user.get('nav') or 'main'
        )

        app.storage.user['panel'] = panel
        app.storage.user['nav'] = nav

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
                with ui.tab_panel('main'):
                    with ui.column().classes('page-center'):
                        ui.label(
                            lang_dict('footer_main', user_lang)
                        ).classes('text-xl q-pa-md')
                        # TODO: контент «Главная»

                # Панель: Поездки
                with ui.tab_panel('trips'):
                    with ui.column().classes('page-center'):
                        ui.label(
                            lang_dict('footer_trips', user_lang)
                        ).classes('text-xl q-pa-md')
                        # TODO: контент «Поездки»

                # Панель: Профиль
                with ui.tab_panel('profile').classes('w-full q-pa-none q-ma-none flex flex-col'):
                    await profile_menu(uid, user_lang, user_data)

                # Панель: Регистрация (скрытая, без кнопки в футере)
                with ui.tab_panel('start_reg_form'):
                    await start_reg_form_ui(uid, user_lang, user_data, choice_role=False)

        # ====================================================================
        # UI: Футер с навигацией
        # ====================================================================
        
        with ui.footer() \
                .bind_visibility_from(
                    app.storage.user, 
                    'panel', 
                    backward=lambda v: v != 'start_reg_form'
                ) \
                .props('bordered') \
                .classes('app-footer no-shadow'):
            
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
                    'trips', 
                    label=lang_dict('footer_trips', user_lang), 
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
                  const doc = document.documentElement;
                  const flagKey = '__main_app_vh_bound';
                  if (doc[flagKey]) { return true; }
                  const telegram = window.Telegram?.WebApp;

                  const updateViewport = () => {
                    const viewport = telegram?.viewportStableHeight || window.innerHeight;
                    doc.style.setProperty('--main-app-viewport', `${viewport}px`);
                    const footer = document.querySelector('.app-footer');
                    if (!footer) { return; }
                    const footerHeight = footer.getBoundingClientRect().height;
                    doc.style.setProperty('--main-footer-height', `${footerHeight}px`);
                  };

                  updateViewport();
                  window.addEventListener('resize', updateViewport);
                  telegram?.onEvent?.('viewportChanged', updateViewport);
                  doc[flagKey] = true;
                  return true;
                })();
                """,
                timeout=3.0,
            )
        except Exception as js_error:
            await log_info(
                f"[page:/main_app][viewport][ОШИБКА] {js_error!r}",
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