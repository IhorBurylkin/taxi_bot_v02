from __future__ import annotations

import asyncio
import os
import re

from starlette.middleware.sessions import SessionMiddleware
from fastapi import FastAPI
from nicegui import ui, app, storage
from web.web_decorators import require_twa, with_theme_toggle, with_svg_splash_img
from web.web_start_reg_form import start_reg_form_ui
from web.web_utilits import get_user_data_uid_lang
from web.splash_animation import splash_screen 

from log.log import log_info

from config.config_utils import lang_dict

step_city = step_phone = step_car = step_docs = None 
_RE_PLATE = re.compile(r'^[A-ZА-ЯІЇЄҐ0-9\- ]{4,12}$', re.IGNORECASE)

NAV_TABS   = ('main', 'trips', 'profile')
PANEL_TABS = ('main', 'trips', 'profile', 'start_reg_form')

# 1) Базовый FastAPI
fastapi_app = FastAPI()

# 2) Прикрепляем NiceGUI к FastAPI
fastapi_app.add_middleware(storage.RequestTrackingMiddleware)
fastapi_app.add_middleware(SessionMiddleware, secret_key=os.getenv('STORAGE_SECRET', 'change-me-please'))

ui.run_with(fastapi_app, storage_secret=os.getenv('STORAGE_SECRET', 'change-me-please'))

# 4) Программный запуск Uvicorn как фоновой задачи
async def start_server(host: str = '0.0.0.0', port: int | None = None):
    try:
        await log_info(f"[server] запуск: host={host}, port={port or os.getenv('NG_PORT', '8080')}", type_msg="info")
        import uvicorn
        port = port or int(os.getenv('NG_PORT', '8080'))
        config = uvicorn.Config(
            app=fastapi_app,
            host=host,
            port=port,
            log_level='info',
        )
        server = uvicorn.Server(config)

        # запускаем сервер как фоновую задачу
        task = asyncio.create_task(server.serve())

        # Дожидаемся старта сокета (короткое ожидание готовности)
        while not getattr(server, 'started', False):
            await asyncio.sleep(0.05)

        await log_info(f"[server] uvicorn стартовал на :{port}", type_msg="info")
        return server, task
    except Exception as e:
        await log_info(f"[server][ОШИБКА] {e!r}", type_msg="error")
        raise


@ui.page('/main_app')
@splash_screen(
    svg_path='/spar_taxi_splash.svg',  # или None для дефолтного
    duration=2000,      # минимальное время показа (мс)
    fade_in=300,        # время появления
    fade_out=500        # время исчезновения
)
@require_twa
@with_theme_toggle
async def main_app():
    try:
        await log_info("[page:/main_app] рендер начат", type_msg="info")

        uid, user_lang, user_data = await get_user_data_uid_lang()
        user_lang = user_lang or 'en'

        # читаем ?tab=... из URL (и не перезагружаем страницу)
        start_tab = await ui.run_javascript(
            "new URLSearchParams(location.search).get('tab') || null"
        )

        # реактивные ключи: 'panel' — активная панель; 'nav' — активная кнопка футера
        panel = (start_tab if start_tab in PANEL_TABS
                 else app.storage.user.get('panel') or 'main')
        nav   = (panel if panel in NAV_TABS
                 else app.storage.user.get('nav') or 'main')

        app.storage.user['panel'] = panel
        app.storage.user['nav']   = nav

        # Контейнер панелей
        with ui.column().classes('w-full q-pa-none q-ma-none'):
            with ui.tab_panels().bind_value(app.storage.user, 'panel') \
                    .props('animated keep-alive transition-prev=fade transition-next=fade') \
                    .classes('w-full')  as panels :
                with ui.tab_panel('main'):
                    with ui.column().classes('page-center'):
                        ui.label(lang_dict('footer_main', user_lang)).classes('text-xl q-pa-md')
                        # TODO: контент «Главная»

                with ui.tab_panel('trips'):
                    with ui.column().classes('page-center'):
                        ui.label(lang_dict('footer_trips', user_lang)).classes('text-xl q-pa-md')
                        # TODO: контент «Поездки»

                with ui.tab_panel('profile'):
                    with ui.column().classes('page-center'):
                        ui.label(lang_dict('footer_profile', user_lang)).classes('text-xl q-pa-md')
                        # TODO: контент «Профиль»

                # СКРЫТАЯ панель регистрации; кнопки в футере для неё нет
                with ui.tab_panel('start_reg_form'):
                    await start_reg_form_ui(uid, user_lang, user_data)

        # Футер (видимые вкладки)
        with ui.footer() \
                .bind_visibility_from(app.storage.user, 'panel', backward=lambda v: v != 'start_reg_form') \
                .props('reveal bordered') \
                .classes('app-footer no-shadow'):
            tabs = (ui.tabs()
                    .bind_value(app.storage.user, 'nav')
                    .props('dense no-caps align=justify narrow-indicator active-color=primary indicator-color=primary')
                    .classes('w-full'))
            with tabs:
                ui.tab('main',    label=lang_dict('footer_main', user_lang),    icon='home').props('stack')
                ui.tab('trips',   label=lang_dict('footer_trips', user_lang),   icon='local_taxi').props('stack')
                ui.tab('profile', label=lang_dict('footer_profile', user_lang), icon='person').props('stack')
            async def _on_nav_change(e):
                panels.set_value(e.value)  # только меняем панели; storage обновится сам
                await ui.run_javascript(f"history.replaceState(null,'','/main_app?tab={e.value}')")
            tabs.on_value_change(_on_nav_change)

        # если пришли по прямой ссылке на /main_app?tab=start_reg_form — синхронизируем футер
        if panel == 'start_reg_form' and app.storage.user.get('nav') != 'main':
            app.storage.user['nav'] = 'main'  # напр., подсвечиваем «Главная»

        await log_info("[page:/main_app] рендер завершён", type_msg="info")
    except Exception as e:
        await log_info(f"[page:/main_app][ОШИБКА] {e!r}", type_msg="error")
        raise
