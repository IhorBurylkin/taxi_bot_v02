from __future__ import annotations

import functools
import asyncio
from nicegui import ui, app 
from fastapi import Request
from functools import wraps
from db.db_utils import update_table, get_user_theme, user_exists, insert_into_table, get_user_data 
from config.config_utils import lang_dict
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES

SDK_SRC = 'https://telegram.org/js/telegram-web-app.js'

# ============================================================================
# API endpoints для темы (инициализация один раз)
# ============================================================================

if not getattr(app.state, 'theme_api_added', False):

    @app.get('/api/theme')
    async def _get_theme(user_id: int | None = None):
        theme = None
        try:
            if user_id:
                theme = await get_user_theme(int(user_id))
        except Exception:
            theme = None
        return {'theme': theme}

    @app.post('/api/theme')
    async def _save_theme(req: Request):
        try:
            payload = await req.json()
        except Exception:
            return {'ok': False, 'error': 'invalid_json'}
        uid = payload.get('user_id')
        theme = payload.get('theme')
        if not (uid and theme in ('light', 'dark')):
            return {'ok': False, 'error': 'bad_params'}
        try:
            uid = int(uid)
            data = {'theme_mode': theme}
            if await user_exists(uid):
                await update_table('users', uid, data)
            else:
                data['user_id'] = uid
                await insert_into_table('users', data)
            return {'ok': True}
        except Exception:
            return {'ok': False, 'error': 'db_error'}

    app.state.theme_api_added = True


# ============================================================================
# CSS Assets
# ============================================================================

def _ensure_theme_assets_once() -> None:
    """Добавляет необходимые CSS стили один раз на клиента"""
    c = ui.context.client
    if c.storage.get('theme_assets_added'):
        return

    # 1) Layout reset - убираем отступы и горизонтальный скролл
    ui.add_head_html('''
<style id="layout-no-padding">
  html, body { 
    margin: 0 !important; 
    padding: 0 !important;
    overflow-x: hidden !important;
  }
  #q-app, .nicegui-content, .q-layout, .q-page-container, .q-page {
    margin: 0 !important; 
    padding: 0 !important;
    width: 100%; 
    max-width: 100%; 
    overflow-x: hidden !important;
  }
</style>
''')

    # 2) Вертикальная прокрутка с правильной высотой
    ui.add_head_html('''
<style id="vscroll-styles">
  .vscroll {
    /* Используем разные единицы для максимальной совместимости */
    height: 100vh;
    height: 100svh; /* Small Viewport Height */
    height: 100dvh; /* Dynamic Viewport Height */
    width: 100%;
    max-width: 100%;
    box-sizing: border-box;
    overflow-y: auto;
    overflow-x: hidden;
    -ms-overflow-style: auto;
    scrollbar-width: auto;
  }
  .q-page, .q-page-container, .nicegui-content { 
    overflow-x: hidden !important; 
  }
</style>
''')

    # 3) Токены темы и стили компонентов
    ui.add_head_html('''
<style id="theme-all-controls">
  /* Токены светлой темы */
  :root {
    --page-bg: #ffffff;
    --field-bg: transparent;
    --field-bg-disabled: transparent;
    --fg: #111827; 
    --fg-muted: #374151; 
    --bg-card: #fff; 
    --border: #e5e7eb;
    --underline: transparent; 
    --underline-focus: #3b82f6; 
    --menu-bg: #fff;
  }
  
  /* Токены темной темы */
  body.body--dark {
    --page-bg: #0b0b0c;
    --field-bg: transparent;
    --field-bg-disabled: transparent;
    --fg: #e5e7eb; 
    --fg-muted: #cfd5dc; 
    --bg-card: #2a2d31; 
    --border: #3c3f46;
    --underline: transparent; 
    --underline-focus: #60a5fa; 
    --menu-bg: #111827;
  }

  /* Карточки и степперы */
  .q-card, .q-stepper {
    background: var(--bg-card) !important; 
    color: var(--fg) !important;
    border: 1px solid var(--border) !important; 
    border-radius: 12px;
    box-shadow: 0 6px 18px rgba(0,0,0,.06);
  }
  
  .q-stepper__title, .q-stepper__label { 
    color: var(--fg) !important; 
  }

  /* Поля ввода */
  .q-field__label { 
    color: var(--fg-muted) !important; 
  }
  
  .q-field__native, .q-field__input {
    color: var(--fg) !important; 
    -webkit-text-fill-color: var(--fg) !important;
  }
  
  .q-select__dropdown-icon { 
    color: var(--fg) !important; 
  }

  /* Фон для filled/outlined */
  .q-field--filled .q-field__control,
  .q-field--outlined .q-field__control {
    background: var(--field-bg) !important;
  }

  /* Disabled состояние */
  .q-field--disabled.q-field--filled .q-field__control,
  .q-field--disabled.q-field--outlined .q-field__control {
    background: var(--field-bg-disabled) !important;
    opacity: 1 !important;
  }

  /* Standard (underlined) */
  .q-field.q-field--standard .q-field__control::before {
    background: var(--underline) !important; 
    opacity: 1 !important;
  }
  
  .q-field.q-field--standard .q-field__control::after {
    background: var(--underline-focus) !important;
  }

  /* Outlined */
  .q-field.q-field--outlined .q-field__control { 
    border-color: var(--border) !important; 
  }

  /* Плейсхолдеры */
  .q-field__native::placeholder, .q-field__input::placeholder {
    color: var(--fg-muted) !important; 
    opacity: .9 !important;
  }

  /* Выпадающие списки */
  body.body--light .q-menu, body.body--dark .q-menu {
    background: var(--menu-bg) !important; 
    color: var(--fg) !important;
  }
  
  .q-menu .q-item, .q-menu .q-item__label { 
    color: var(--fg) !important; 
  }

  /* Радио/чекбоксы/тогглы */
  .q-radio__label, .q-checkbox__label, .q-toggle__label { 
    color: var(--fg) !important; 
  }
                     
  /* Фон страницы */
  html, body, #q-app, .q-layout, .q-page-container, .q-page {
    background: var(--page-bg) !important;
    color: var(--fg) !important;
  }
  
  /* Контейнеры */
  #q-app, .q-layout, .q-page-container, .q-page, .nicegui-content {
    color: var(--fg) !important;
  }

  /* Вкладки футера */
  .q-tabs .q-tab__label,
  .q-tabs .q-tab__icon {
    color: var(--fg) !important;
  }

  .q-tabs .q-tab:not(.q-tab--active) .q-tab__label,
  .q-tabs .q-tab:not(.q-tab--active) .q-tab__icon {
    color: var(--fg) !important;
    opacity: .86;
  }
  
  /* Прозрачный футер */
  .app-footer {
    background: transparent !important;
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
    border-top: 0 !important;
  }
</style>
''')

    c.storage['theme_assets_added'] = True


# ============================================================================
# Вспомогательные функции
# ============================================================================

async def _resolve_user_lang() -> str:
    """Возвращает язык пользователя из БД"""
    try:
        from web.web_utilits import _get_uid
        uid = await _get_uid()
        if uid:
            row = await get_user_data('users', uid)
            lang = (row or {}).get('language')
            if isinstance(lang, str):
                lang = lang.lower()
                if lang in (SUPPORTED_LANGUAGES or []):
                    return lang
    except Exception:
        pass
    return (DEFAULT_LANGUAGES or 'en').lower()


def _add_theme_toggle_ui(user_lang: str) -> None:
    """Добавляет переключатель темы в интерфейс"""
    _ensure_theme_assets_once()

    # Синхронизация положения тумблера после загрузки темы
    ui.run_javascript('''
(function(){
  function alignToggle(){
    const root = document.querySelector('[data-theme-toggle]');
    const toggle = root?.querySelector?.('.q-toggle[role="switch"]') || root;
    if (!toggle) return;
    
    const isDark = !!(window.Quasar?.Dark?.isActive);
    const toggleState = toggle.getAttribute('aria-checked') === 'true';
    
    if (toggleState !== isDark) {
      window.__syncing_theme_toggle = true;
      toggle.click();
      setTimeout(() => { window.__syncing_theme_toggle = false; }, 0);
    }
  }
  
  if (window.__THEME_BOOT_DONE) {
    alignToggle();
  } else {
    window.addEventListener('theme:ready', alignToggle, {once: true});
  }
})();
''')

    with ui.element('div').style('position:fixed;top:12px;right:12px;z-index:2000;'):
        sw = (
            ui.switch('', value=False)
              .props('dense color=grey-8 size=md rounded')
              .props('checked-icon=dark_mode unchecked-icon=light_mode')
              .tooltip(lang_dict('theme_switcher', user_lang))
        )
        sw.props('data-theme-toggle')

        def _on_change(e):
            to_dark = bool(e.value)
            js = """
if (window.__syncing_theme_toggle) return;

const dark = %s;
const desired = dark ? 'dark' : 'light';

// Сохранить выбор
try { localStorage.setItem('theme_override', desired); } catch {}

// Применить тему через Quasar
try { window.Quasar?.Dark?.set?.(dark); } catch {}

// Классы на body
const body = document.body;
body.classList.toggle('body--dark', dark);
body.classList.toggle('body--light', !dark);

// Цветовая схема и фон
const bg = dark ? '#0b0b0c' : '#ffffff';
try {
  document.documentElement.style.setProperty('color-scheme', dark ? 'dark' : 'light');
  document.documentElement.style.backgroundColor = bg;
  body.style.backgroundColor = bg;
  window.Telegram?.WebApp?.setBackgroundColor?.(bg);
} catch {}

// Safari WebKit fix для цвета текста в инпутах
try {
  const inputs = document.querySelectorAll('.q-field__native, .q-field__input');
  inputs.forEach(el => {
    el.style.webkitTextFillColor = getComputedStyle(el).color;
  });
} catch {}

// Асинхронное сохранение в БД через sendBeacon
const uid = window.Telegram?.WebApp?.initDataUnsafe?.user?.id || localStorage.getItem('tg_user_id');
if (uid) {
  const blob = new Blob(
    [JSON.stringify({ user_id: uid, theme: desired })], 
    {type: 'application/json'}
  );
  navigator.sendBeacon('/api/theme', blob);
}
            """ % ('true' if to_dark else 'false')
            ui.run_javascript(js)

        sw.on_value_change(_on_change)


# ============================================================================
# Инициализация Telegram WebApp
# ============================================================================

def ensure_twa() -> None:
    """
    Инициализация Telegram WebApp с детерминированной загрузкой темы.
    
    Процесс:
    1. Загрузка Telegram WebApp SDK
    2. Pre-paint: установка цвета фона до первого рендера (предотвращает мигание)
    3. Определение темы из:
       - localStorage.theme_override (выбор пользователя)
       - window.matchMedia (системная тема)
    4. Применение классов body--dark/body--light для CSS переменных
    5. Синхронизация с Quasar Dark mode
    6. Установка backgroundColor через Telegram WebApp API
    7. Загрузка сохранённой темы из БД (асинхронно)
    8. Событие 'theme:ready' для координации с UI компонентами
    
    Критично: функция идемпотентна (можно вызывать многократно)
    """
    c = ui.context.client
    
    # Шаг 1: SDK загружается один раз на клиента
    if not c.storage.get('twa_sdk_added'):
        ui.add_head_html('<script src="https://telegram.org/js/telegram-web-app.js"></script>')
        ui.add_head_html(
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1, maximum-scale=1, '
            'user-scalable=no, viewport-fit=cover">'
        )
        c.storage['twa_sdk_added'] = True
    
    # Шаг 2-8: Boot-скрипт загружается один раз    
    if not c.storage.get('theme_boot_added'):
<<<<<<< HEAD
        ui.run_javascript(r"""
        (function(){
          // PRE-PAINT: Устанавливаем фон ДО первого рендера
          // Это предотвращает белую вспышку на тёмной теме
          var ov=null; 
          try { 
            ov=localStorage.getItem('theme_override'); 
          } catch(_) {}
          
          // Определяем желаемую тему
          var preferDark = ov ? (ov==='dark')
                              : (window.matchMedia && 
                                 matchMedia('(prefers-color-scheme: dark)').matches);
          var desired = preferDark ? 'dark' : 'light';
          var isDark = (desired==='dark');

          try {
            // Применяем цвета немедленно
            document.documentElement.style.backgroundColor = 
              isDark ? '#0b0b0c' : '#ffffff';
            document.body.style.backgroundColor = 
              isDark ? '#0b0b0c' : '#ffffff';
            document.documentElement.style.setProperty(
              'color-scheme', 
              isDark ? 'dark' : 'light'
            );
            
            // Синхронизируем классы и Quasar
            document.body.classList.toggle('body--dark', isDark);
            document.body.classList.toggle('body--light', !isDark);
            window.Quasar?.Dark?.set?.(isDark);
            
            // Телеграм фон
            window.Telegram?.WebApp?.setBackgroundColor?.(
              isDark ? '#0b0b0c' : '#ffffff'
            );
          } catch(e) {
            console.error('[ensure_twa] Pre-paint failed:', e);
          }

          // Асинхронная загрузка темы из БД (не блокирует рендер)
          (async () => {
            try {
              const uid = window.Telegram?.WebApp?.initDataUnsafe?.user?.id;
              if (uid) {
                const resp = await fetch(`/api/theme?user_id=${uid}`);
                const data = await resp.json();
                if (data.theme && data.theme !== desired) {
                  // Применяем тему из БД, если отличается
                  const dbIsDark = (data.theme === 'dark');
                  document.body.classList.toggle('body--dark', dbIsDark);
                  document.body.classList.toggle('body--light', !dbIsDark);
                  window.Quasar?.Dark?.set?.(dbIsDark);
                  // ... (остальная синхронизация)
                }
              }
            } catch(e) {
              console.warn('[ensure_twa] DB theme load failed:', e);
            }
          })().finally(() => {
            // Сигнализируем готовность темы
=======
    # 1) pre-paint можно оставить как CSS в head (ui.add_head_html(...style...))
    # 2) сам boot — запускать КОДОМ
        ui.run_javascript(r"""
        (function(){
          // --- PRE-PAINT (как у вас): bg + color-scheme ---
          var ov=null; try{ ov=localStorage.getItem('theme_override'); }catch(_){}
          var preferDark = ov ? (ov==='dark')
                              : (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
          var desired = preferDark ? 'dark' : 'light';
          var isDark = (desired==='dark');

          try{
            document.documentElement.style.backgroundColor = isDark ? '#0b0b0c' : '#ffffff';
            document.body.style.backgroundColor = isDark ? '#0b0b0c' : '#ffffff';
            document.documentElement.style.setProperty('color-scheme', isDark ? 'dark' : 'light');
            // Применяем сразу классы и Quasar
            document.body.classList.toggle('body--dark', isDark);
            document.body.classList.toggle('body--light', !isDark);
            window.Quasar?.Dark?.set?.(isDark);
            // Телеграм фон
            window.Telegram?.WebApp?.setBackgroundColor?.(isDark ? '#0b0b0c' : '#ffffff');
          }catch(e){}

          // Попытка подтянуть user_id и тему из БД (если есть) — необязателен для «моментального» применения
          (async () => {
            // ... ваша логика получения uid и fetch('/api/theme?user_id=...') ...
            // при ответе 'light'/'dark' — переустановить классы/Quasar/цвет
          })().finally(() => {
>>>>>>> 1b9d460f37ca78897db96c09acd32a2a41eb3aba
            window.__THEME_BOOT_DONE = true;
            window.dispatchEvent(new Event('theme:ready'));
          });
        })();
        """)
        c.storage['theme_boot_added'] = True


# ============================================================================
# Декораторы
# ============================================================================

def require_twa(fn):
    """Декоратор для обязательной инициализации Telegram WebApp"""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ensure_twa()
        return await fn(*args, **kwargs)
    return wrapper


def with_theme_toggle(fn):
    """Декоратор для добавления переключателя темы"""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        _ensure_theme_assets_once()
        user_lang = await _resolve_user_lang()
        _add_theme_toggle_ui(user_lang)
        return await fn(*args, **kwargs)
    return wrapper


def with_svg_splash_img(
    *,
    svg_path: str | None = None,
    fade_ms: int = 500,
    z_index: int = 2_147_483_000,
    auto_hide: bool = True,
    hide_delay_ms: int = 100,
):
    """Декоратор для отображения splash screen"""
    def _decorator(fn):
        @wraps(fn)
        async def _wrapped(*args, **kwargs):
            from web.web_utilits import get_spar_taxi_splash_svg, show_startup_splash_img_min
            
            # Показать splash
            svg = get_spar_taxi_splash_svg(svg_path)
            overlay, hide = show_startup_splash_img_min(
                svg, 
                fade_ms=fade_ms, 
                z_index=z_index
            )
            
            # Убедиться, что overlay находится на верхнем уровне DOM
            ov_id = overlay.id
            ui.run_javascript(f"""
(async () => {{
  // Небольшая задержка, чтобы DOM успел отрендериться
  await new Promise(r => setTimeout(r, 10));
  
  const el = document.querySelector('[data-id="{ov_id}"]');
  if (!el) return;
  
  // Переместить в body, если не там
  if (el.parentNode !== document.body) {{
    document.body.appendChild(el);
  }}
  
  // Гарантировать правильное позиционирование
  Object.assign(el.style, {{
    position: 'fixed',
    top: '0',
    left: '0',
    right: '0',
    bottom: '0',
    margin: '0',
    padding: '0',
    transform: 'none',
    WebkitTransform: 'none',
    opacity: '1',
    pointerEvents: 'auto'
  }});
}})();
""")
            
            try:
                # Выполнить основную функцию страницы
                result = await fn(*args, **kwargs)
            finally:
                # Скрыть splash
                if auto_hide:
                    await asyncio.sleep(hide_delay_ms / 1000)
                    await hide()
            
            return result
        return _wrapped
    return _decorator