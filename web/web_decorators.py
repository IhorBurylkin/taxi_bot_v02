from __future__ import annotations

import functools
import asyncio
from nicegui import ui, app 
from fastapi import Request
from functools import wraps
from db.db_utils import update_table, get_user_theme, user_exists, insert_into_table, get_user_data 
from config.config_utils import lang_dict
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES
from log.log import log_info

SDK_SRC = 'https://telegram.org/js/telegram-web-app.js'

# ============================================================================
# API endpoints для темы (инициализация один раз)
# ============================================================================

if not getattr(app.state, 'theme_api_added', False):

    @app.get('/api/theme')
    async def _get_theme(user_id: int | None = None):
        try:
            if not user_id:
                await log_info('[api/theme][GET] параметр user_id отсутствует', type_msg='warning')
                return {'theme': None}

            theme: str | None = None
            try:
                theme = await get_user_theme(int(user_id))
                await log_info(f'[api/theme][GET] uid={user_id} тема={theme}', type_msg='info')
            except Exception as db_error:
                await log_info(f'[api/theme][GET][ОШИБКА] uid={user_id} {db_error!r}', type_msg='error')
                theme = None

            return {'theme': theme}
        except Exception as unexpected_error:
            await log_info(f'[api/theme][GET][ОШИБКА] {unexpected_error!r}', type_msg='error')
            return {'theme': None}

    @app.post('/api/theme')
    async def _save_theme(req: Request):
        try:
            payload = await req.json()
        except Exception as parse_error:
            await log_info(f'[api/theme][POST][ОШИБКА] не удалось разобрать JSON: {parse_error!r}', type_msg='error')
            return {'ok': False, 'error': 'invalid_json'}

        uid_raw = payload.get('user_id')
        theme = payload.get('theme')
        if not (uid_raw and theme in ('light', 'dark')):
            await log_info(f'[api/theme][POST] некорректные параметры: uid={uid_raw}, theme={theme}', type_msg='warning')
            return {'ok': False, 'error': 'bad_params'}

        try:
            uid = int(uid_raw)
        except (TypeError, ValueError) as cast_error:
            await log_info(f'[api/theme][POST] uid не является числом: {uid_raw!r} ({cast_error!r})', type_msg='error')
            return {'ok': False, 'error': 'bad_params'}

        try:
            data = {'theme_mode': theme}
            if await user_exists(uid):
                await update_table('users', uid, data)
            else:
                data['user_id'] = uid
                await insert_into_table('users', data)
            await log_info(f'[api/theme][POST] uid={uid} сохранена тема={theme}', type_msg='info')
            return {'ok': True}
        except Exception as db_error:
            await log_info(f'[api/theme][POST][ОШИБКА] uid={uid} {db_error!r}', type_msg='error')
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
  
  /* Футер: непрозрачный контейнер с верхней границей */
  .app-footer {
    background: var(--bg-card) !important;
    border-top: 1px solid var(--border) !important;
    box-shadow: 0 -4px 18px rgba(0,0,0,.08) !important;
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
  }

  /* Главная страница: контейнер вкладок с ограничением по высоте */
  .main-app-content {
    flex: 1 1 auto;
    min-height: 0;
    height: calc(var(--main-app-viewport, 100vh) - var(--main-footer-height, 64px));
    overflow-y: auto;
  }

  /* Профиль: карточки и элементы меню */
  .profile-header-card {
    background: linear-gradient(180deg, rgba(46,204,113,.14) 0%, rgba(46,204,113,0) 90%);
    border: 1px solid rgba(46,204,113,.24) !important;
    box-shadow: none !important;
  }

  .profile-avatar {
    border: 3px solid rgba(46,204,113,.35);
    background-color: transparent !important;
  }

  .profile-menu-card {
    border-radius: 14px;
    box-shadow: none !important;
    border: 1px solid var(--border) !important;
  }

  .profile-menu-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px;
    border-bottom: 1px solid rgba(0,0,0,.08);
  }

  .profile-menu-item:last-child {
    border-bottom: none;
  }

  body.body--dark .profile-menu-item {
    border-bottom: 1px solid rgba(255,255,255,.08);
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
    
    # Шаг 2-8: скрипт инициализации темы выполняется при каждом заходе, но сам себя делает идемпотентным
    ui.run_javascript(r"""
    (function(){
      if (window.__THEME_BOOTSTRAP_INITIALIZED) {
        return;
      }
      window.__THEME_BOOTSTRAP_INITIALIZED = true;

      const state = {
        quasarTimer: null,
        pending: null,
      };

      const syncQuasarDark = (dark) => {
        try {
          if (window.Quasar?.Dark) {
            window.Quasar.Dark.set(dark);
            return true;
          }
        } catch (_) {}
        return false;
      };

      const applyTheme = (theme) => {
        if (theme !== 'dark' && theme !== 'light') { return; }
        const dark = theme === 'dark';
        try {
          document.documentElement.style.backgroundColor = dark ? '#0b0b0c' : '#ffffff';
          document.body.style.backgroundColor = dark ? '#0b0b0c' : '#ffffff';
          document.documentElement.style.setProperty('color-scheme', dark ? 'dark' : 'light');
          document.documentElement.setAttribute('data-theme', theme);
        } catch (_) {}
        try {
          document.body.classList.toggle('body--dark', dark);
          document.body.classList.toggle('body--light', !dark);
        } catch (_) {}
        if (!syncQuasarDark(dark)) {
          if (state.quasarTimer) {
            clearInterval(state.quasarTimer);
            state.quasarTimer = null;
          }
          state.quasarTimer = setInterval(() => {
            if (syncQuasarDark(dark)) {
              clearInterval(state.quasarTimer);
              state.quasarTimer = null;
            }
          }, 40);
          setTimeout(() => {
            if (state.quasarTimer) {
              clearInterval(state.quasarTimer);
              state.quasarTimer = null;
            }
          }, 4000);
        }
        try { window.Telegram?.WebApp?.setBackgroundColor?.(dark ? '#0b0b0c' : '#ffffff'); } catch (_) {}
        try {
          const inputs = document.querySelectorAll('.q-field__native, .q-field__input');
          inputs.forEach((el) => { el.style.webkitTextFillColor = getComputedStyle(el).color; });
        } catch (_) {}
        window.__THEME_LAST = theme;
        window.dispatchEvent(new CustomEvent('theme:applied', { detail: { theme } }));
      };

      const readOverride = () => {
        try {
          const stored = localStorage.getItem('theme_override');
          if (stored === 'dark' || stored === 'light') {
            return stored;
          }
        } catch (_) {}
        return null;
      };

      const detectPreferred = () => {
        const override = readOverride();
        if (override) {
          return override;
        }
        try {
          const mq = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
          if (mq && mq.matches) {
            return 'dark';
          }
        } catch (_) {}
        return 'light';
      };

      const rememberUid = (uid) => {
        try { localStorage.setItem('tg_user_id', String(uid)); } catch (_) {}
      };

      const resolveUid = async () => {
        const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        for (let i = 0; i < 80; i += 1) {
          try {
            const direct = window?.Telegram?.WebApp?.initDataUnsafe?.user?.id;
            if (direct) {
              return direct;
            }
          } catch (_) {}
          await wait(15);
        }
        try {
          const fallback = localStorage.getItem('tg_user_id');
          if (fallback) {
            return fallback;
          }
        } catch (_) {}
        return null;
      };

      const fetchThemeFromDb = async (uid) => {
        try {
          const response = await fetch(`/api/theme?user_id=${encodeURIComponent(uid)}`, { method: 'GET', cache: 'no-store' });
          if (!response.ok) {
            return null;
          }
          const data = await response.json();
          const incoming = data?.theme;
          if (incoming === 'dark' || incoming === 'light') {
            return incoming;
          }
          return null;
        } catch (_) {
          return null;
        }
      };

      const reapply = async () => {
        if (state.pending) {
          return state.pending;
        }
        state.pending = (async () => {
          const preferred = detectPreferred();
          applyTheme(preferred);

          const override = readOverride();
          if (override) {
            applyTheme(override);
          }

          const uid = await resolveUid();
          if (!uid) {
            return;
          }
          rememberUid(uid);

          const fromDb = await fetchThemeFromDb(uid);
          if (fromDb) {
            if (fromDb !== override) {
              try { localStorage.setItem('theme_override', fromDb); } catch (_) {}
              applyTheme(fromDb);
            } else {
              try { localStorage.setItem('theme_override', fromDb); } catch (_) {}
            }
          }
        })();
        try {
          await state.pending;
        } finally {
          state.pending = null;
        }
      };

      window.__THEME_BOOTSTRAP = {
        applyTheme,
        detectPreferred,
        reapply,
      };
    })();
    """)

    ui.run_javascript(r"""
    (async function(){
      if (!window.__THEME_BOOTSTRAP?.reapply) {
        return;
      }
      window.__THEME_BOOT_DONE = false;
      try {
        await window.__THEME_BOOTSTRAP.reapply();
      } finally {
        window.__THEME_BOOT_DONE = true;
        window.dispatchEvent(new Event('theme:ready'));
      }
    })();
    """)


# ============================================================================
# Декораторы
# ============================================================================

def require_twa(fn):
    """Декоратор для обязательной инициализации Telegram WebApp"""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            # Обеспечиваем загрузку SDK и применение темы перед выполнением обработчика
            await log_info('[require_twa] декоратор вызван', type_msg='debug')
            ensure_twa()
            await log_info('[require_twa] ensure_twa выполнен', type_msg='debug')
        except Exception as error:
            await log_info(f'[require_twa][ОШИБКА] {error!r}', type_msg='error')
            raise

        try:
            return await fn(*args, **kwargs)
        except Exception as error:
            await log_info(f'[require_twa][fn][ОШИБКА] {error!r}', type_msg='error')
            raise
    return wrapper


def on_toggle(render_toggle: bool = True):
    """
    Управляет добавлением UI-переключателя темы.
    True  → всё как раньше: стили/boot + сам тумблер
    False → стили/boot остаются, НО сам тумблер не рендерится
    """
    def _decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                _ensure_theme_assets_once()
                # язык всё равно резолвим — может пригодиться в самом view
                user_lang = await _resolve_user_lang()
                if render_toggle:
                    _add_theme_toggle_ui(user_lang)
                return await fn(*args, **kwargs)
            except Exception as e:
                # Логировать по правилам проекта
                try:
                    await log_info(f"on_toggle wrapper failed: {e}", type_msg="error")
                except Exception:
                    pass
                raise
        return wrapper
    return _decorator


def with_theme_toggle(arg=None):
    """
    Обратная совместимость и «сахар»:
      @with_theme_toggle            → рендерим тумблер (по умолчанию)
      @with_theme_toggle(False)     → не рендерим тумблер
      @with_theme_toggle(True)      → рендерим тумблер
    """
    # форма @with_theme_toggle
    if callable(arg):
        return on_toggle(True)(arg)

    # форма @with_theme_toggle(False/True) или без аргумента
    render_toggle = True if arg is None else bool(arg)
    return on_toggle(render_toggle)
