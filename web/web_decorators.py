import functools
import asyncio
from nicegui import ui, app 
from fastapi import Request
from functools import wraps
from db.db_utils import update_table, get_user_theme, user_exists, insert_into_table, get_user_data 
from config.config_utils import lang_dict
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES
from web.web_utilits import _get_uid, show_startup_splash_img_min, get_spar_taxi_splash_svg

SDK_SRC = 'https://telegram.org/js/telegram-web-app.js'

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

def require_twa(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ensure_twa()
        return await fn(*args, **kwargs)
    return wrapper


# ------------------------------- CSS / ASSETS --------------------------------


def _ensure_theme_assets_once() -> None:
    c = ui.context.client
    if c.storage.get('theme_assets_added'):
        return

    # 1) layout reset (убрать отступы, запретить горизонтальный скролл)
    ui.add_head_html('''
<style id="layout-no-padding">
  html, body { margin:0 !important; padding:0 !important; }
  #q-app, .nicegui-content, .q-layout, .q-page-container, .q-page {
    margin:0 !important; padding:0 !important;
    width:100%; max-width:100%; overflow-x:hidden !important;
  }
</style>
''')

    # 2) вертикальная прокрутка (скрытый скроллбар, только вертикаль)
    ui.add_head_html('''
<style id="vscroll-styles">
  .vscroll{
    height:100svh; width:100%; max-width:100%;
    box-sizing:border-box;
    overflow-y:auto; overflow-x:hidden;
    /* стандартные системные полосы */
    -ms-overflow-style:auto;      /* IE/Edge Legacy */
    scrollbar-width:auto;         /* Firefox */
  }
  /* удаляем спец-правило ::-webkit-scrollbar */
  /* .vscroll::-webkit-scrollbar{ ... } — удалить */
  /* опционально: вообще убираем управление gutter'ом */
  /* .vscroll{ scrollbar-gutter:auto; } — или просто не задавать */
  .q-page, .q-page-container, .nicegui-content { overflow-x:hidden !important; }
</style>
''')

#     ui.add_head_html('''
# <style id="page-center">
#   /* Горизонтальное центрирование контента без вмешательства в вертикальные отступы */
#   .page-center{
#     box-sizing:border-box;
#     margin-left:auto;               /* центрировать: margin: 0 auto */
#     margin-right:auto;
#     width:100%;
#     max-width: 760px;               /* целевая ширина контента, меняйте по вкусу */
#     padding-left:16px;              /* только горизонтальные внутренние поля */
#     padding-right:16px;
#   }
#   @media (min-width: 768px){ .page-center{ padding-left:24px; padding-right:24px; } }
#   @media (min-width: 1200px){ .page-center{ padding-left:32px; padding-right:32px; } }
# </style>
# ''')


    # 3) единая тема для ВСЕХ контролов (светлая/тёмная)
    ui.add_head_html('''
<style id="theme-all-controls">
  /* токены */
  :root  {
    --page-bg:#ffffff;
    --field-bg:transparent;               /* фон активного инпута/select в light */
    --field-bg-disabled:transparent;      /* фон disabled в light */
    --fg:#111827; --fg-muted:#374151; --bg-card:#fff; --border:#e5e7eb;
    --underline:transparent; --underline-focus:#3b82f6; --menu-bg:#fff;
  }
  body.body--dark  {
    --page-bg:#0b0b0c;
    --field-bg:transparent;               /* фон активного в dark */
    --field-bg-disabled:transparent;      /* фон disabled в dark */ 
    --fg:#e5e7eb; --fg-muted:#cfd5dc; --bg-card:#2a2d31; --border:#3c3f46;
    --underline:transparent; --underline-focus:#60a5fa; --menu-bg:#111827;
  }

  /* карточки/степпер */
  .q-card, .q-stepper {
    background:var(--bg-card)!important; color:var(--fg)!important;
    border:1px solid var(--border)!important; border-radius:12px;
    box-shadow:0 6px 18px rgba(0,0,0,.06);
  }
  .q-stepper__title, .q-stepper__label { color:var(--fg)!important; }

  /* поля ввода (QInput/QSelect/QField) */
  .q-field__label { color:var(--fg-muted)!important; }
  .q-field__native, .q-field__input {
    color:var(--fg)!important; -webkit-text-fill-color:var(--fg)!important;
  }
  .q-select__dropdown-icon { color:var(--fg)!important; }

  /* заливка для filled/outlined */
  .q-field--filled .q-field__control,
  .q-field--outlined .q-field__control{
    background:var(--field-bg)!important;
  }

  /* фон для disabled состояний */
  .q-field--disabled.q-field--filled .q-field__control,
  .q-field--disabled.q-field--outlined .q-field__control{
    background:var(--field-bg-disabled)!important;
    opacity:1!important; /* не «блекнуть», если нужно */
  }

  /* standard (underlined) */
  .q-field.q-field--standard .q-field__control::before {
    background:var(--underline)!important; opacity:1!important;
  }
  .q-field.q-field--standard .q-field__control::after {
    background:var(--underline-focus)!important;
  }

  /* outlined */
  .q-field.q-field--outlined .q-field__control { border-color:var(--border)!important; }

  /* плейсхолдеры */
  .q-field__native::placeholder, .q-field__input::placeholder {
    color:var(--fg-muted)!important; opacity:.9!important;
  }

  /* выпадающие списки (QMenu от QSelect) */
  body.body--light .q-menu, body.body--dark .q-menu {
    background:var(--menu-bg)!important; color:var(--fg)!important;
  }
  .q-menu .q-item, .q-menu .q-item__label { color:var(--fg)!important; }

  /* радио/чекбоксы/тогглы */
  .q-radio__label, .q-checkbox__label, .q-toggle__label { color:var(--fg)!important; }
                     
  /* фон страницы — всегда из токена */
  html, body, #q-app, .q-layout, .q-page-container, .q-page{
    background:var(--page-bg) !important;
  }
  /* базовый цвет текста для всей страницы (страховка) */
  html, body { color: var(--fg) !important; }
</style>
''')
    
    ui.add_head_html('''
<style id="theme-fg-enforce">
  /* Базовый цвет текста для всех контейнеров Quasar/NiceGUI */
  #q-app, .q-layout, .q-page-container, .q-page, .nicegui-content {
    color: var(--fg) !important;
  }

  /* Иконки и подписи во вкладках (включая футер) — из токена темы */
  .q-tabs .q-tab__label,
  .q-tabs .q-tab__icon {
    color: var(--fg) !important;
  }

  /* Неактивные вкладки — слегка приглушаем, но тоже из --fg */
  .q-tabs .q-tab:not(.q-tab--active) .q-tab__label,
  .q-tabs .q-tab:not(.q-tab--active) .q-tab__icon {
    color: var(--fg) !important;
    opacity: .86;
  }
</style>
''')
    
    ui.add_head_html('''
<style id="app-footer-transparent">
  .app-footer{
    background: transparent !important;
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
    border-top: 0 !important; /* убери строку, если хочешь оставить .props('bordered') */
  }
</style>
''')

    #ui.add_head_html('<style id="pre-theme-veil">body{visibility:hidden}</style>')

    c.storage['theme_assets_added'] = True


# ------------------------------- TWA / THEME ---------------------------------

async def _resolve_user_lang() -> str:
    """Возвращает язык пользователя из БД; если нет/неподдерживается — DEFAULT_LANGUAGES."""
    try:
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
    """Тумблер темы: синхронизация после theme:ready и корректное переключение."""
    _ensure_theme_assets_once()

    # синхронизация положения тумблера, когда бут темы завершён
    ui.run_javascript('''
(function(){
  function align(){
    const root = document.querySelector('[data-theme-toggle]');
    const q = root
      ? (root.matches?.('.q-toggle[role="switch"]') ? root
         : root.querySelector?.('.q-toggle[role="switch"]'))
      : null;
    if (!q) return;
    const isDark = !!(window.Quasar?.Dark?.isActive);
    const now = q.getAttribute('aria-checked') === 'true';
    if (now !== isDark) {
      window.__syncing_theme_toggle = true;
      q.click();
      setTimeout(()=>{ window.__syncing_theme_toggle = false; }, 0);
    }
  }
  if (window.__THEME_BOOT_DONE) align();
  else window.addEventListener('theme:ready', align, {once:true});
})();''')

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

                  try { localStorage.setItem('theme_override', desired); } catch {}
                  try { window.Quasar?.Dark?.set?.(dark); } catch {}

                  // единая точка правды: классы на <body>
                  const b = document.body;
                  b.classList.toggle('body--dark',  dark);
                  b.classList.toggle('body--light', !dark);

                  // color-scheme + фон документа и Mini App
                  try {
                    const bg = dark ? '#0b0b0c' : '#ffffff';
                    document.documentElement.style.setProperty('color-scheme', dark ? 'dark' : 'light');
                    document.documentElement.style.backgroundColor = bg;
                    b.style.backgroundColor = bg;
                    window.Telegram?.WebApp?.setBackgroundColor?.(bg);
                  } catch {}

                  // Safari/WebKit: пересчёт -webkit-text-fill-color у инпутов
                  try {
                    const els = document.querySelectorAll('.q-field__native, .q-field__input');
                    els.forEach(el => { el.style.webkitTextFillColor = getComputedStyle(el).color; });
                  } catch {}

                  // async сохранение в БД
                  const uid = (window.Telegram?.WebApp?.initDataUnsafe?.user?.id) || localStorage.getItem('tg_user_id');
                  if (uid) {
                    const blob = new Blob([JSON.stringify({ user_id: uid, theme: desired })], {type:'application/json'});
                    navigator.sendBeacon('/api/theme', blob);
                  }
                  """
            ui.run_javascript(js % ('true' if to_dark else 'false'))

        sw.on_value_change(_on_change)


def with_theme_toggle(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        _ensure_theme_assets_once()
        user_lang = await _resolve_user_lang()
        _add_theme_toggle_ui(user_lang)
        # НЕ трогаем ui.dark_mode — тема уже задана синхронным бутом
        return await fn(*args, **kwargs)
    return wrapper


def ensure_twa() -> None:
    """Детерминированный бут темы: Telegram SDK, pre-paint и симметричное применение dark/light."""
    c = ui.context.client

    if not c.storage.get('twa_sdk_added'):
        ui.add_head_html('<script src="https://telegram.org/js/telegram-web-app.js"></script>')
        ui.add_head_html(
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        )
        c.storage['twa_sdk_added'] = True
    if not c.storage.get('theme_boot_added'):
        ui.add_head_html('''
        <script id="theme-boot">
        (function(){
          // --- PRE-PAINT: мгновенно красим фон по последней теме/системной схеме ---
          var ov=null; try{ ov = localStorage.getItem('theme_override'); }catch(_){}
          var preferDark = ov ? (ov==='dark')
                              : (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
          var preBg = preferDark ? '#0b0b0c' : '#ffffff';
          try{ document.documentElement.style.backgroundColor = preBg; }catch(_){}
          try{ document.body.style.backgroundColor = preBg; }catch(_){}
          try{ document.documentElement.style.setProperty('color-scheme', preferDark ? 'dark' : 'light'); }catch(_){}
          try{ window.Telegram?.WebApp?.setBackgroundColor?.(preBg); }catch(_){}
          try{ document.body.style.visibility = 'hidden'; }catch(_){}
          // -------------------------------------------------------------------------

          const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));
          const deadline = performance.now() + 1200;

          async function getUserId(){
            let tries = 0;
            while (performance.now() < deadline && tries < 60){
              try{
                const tg = window.Telegram && Telegram.WebApp;
                tg && tg.ready && tg.ready(); tg && tg.expand && tg.expand();
                const id = tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id;
                if (id) return id;
              }catch(_){}
              tries++; await sleep(16);
            }
            return null;
          }

          async function fetchTheme(uid){
            for (let i=0; i<3; i++){
              try{
                const r = await fetch('/api/theme?user_id='+uid, {cache:'no-store'});
                if (r.ok){
                  const j = await r.json();
                  if (j && (j.theme==='dark' || j.theme==='light')) return j.theme;
                }
              }catch(_){}
              await sleep(50);
            }
            return null;
          }

          (async function boot(){
            let desired = 'light';
            let uid = await getUserId();
            if (uid) {
              try { localStorage.setItem('tg_user_id', String(uid)); } catch {}
              try { document.dispatchEvent(new CustomEvent('tg:user', { detail: { id: uid } })); } catch {}
            }

            if (uid){
              const fromDb = await fetchTheme(uid);
              desired = (fromDb==='dark' || fromDb==='light') ? fromDb
                      : (localStorage.getItem('theme_override') || 'light');
            } else {
              try{ desired = localStorage.getItem('theme_override') || 'light'; }catch(_){}
            }
            try{ localStorage.setItem('theme_override', desired); }catch(_){}

            const dark = (desired === 'dark');
            const bg = dark ? '#0b0b0c' : '#ffffff';

            // Quasar и классы body — симметрично
            try { window.Quasar?.Dark?.set?.(dark); } catch {}
            try {
              document.body.classList.toggle('body--dark',  dark);
              document.body.classList.toggle('body--light', !dark);
            } catch {}

            // color-scheme + фон документа и Mini App
            try {
              document.documentElement.style.setProperty('color-scheme', dark ? 'dark' : 'light');
              document.documentElement.style.backgroundColor = bg;
              document.body.style.backgroundColor = bg;
              window.Telegram?.WebApp?.setBackgroundColor?.(bg);
            } catch {}

            // снять вуаль и отдать сигнал готовности
            try{ document.getElementById('pre-theme-veil')?.remove(); }catch(_){}
            try{ document.body.style.visibility = 'visible'; }catch(_){}
            window.__THEME_BOOT_DONE = true;
            window.dispatchEvent(new Event('theme:ready'));
          })();
        })();
        </script>''')
        c.storage['theme_boot_added'] = True


def with_svg_splash_img(
    *,
    svg_path: str | None = None,      # если None — возьмём встроенный дефолт из utilits
    fade_ms: int = 500,
    z_index: int = 2_147_483_000,
    auto_hide: bool = True,
    hide_delay_ms: int = 100,         # задержка, чтобы кадр успел отрендериться
):
    """Декоратор-«шапка» сплэша для page-builder-функций NiceGUI.
    Использует show_startup_splash_img_min (без JS, 100vh/svh/dvh)."""
    def _decorator(fn):
        @wraps(fn)
        async def _wrapped(*args, **kwargs):
            svg = get_spar_taxi_splash_svg(svg_path)
            overlay, hide = show_startup_splash_img_min(svg, fade_ms=fade_ms, z_index=z_index)
            ov_id = overlay.id
            ui.run_javascript(f"""
            (() => {{
              const el = document.querySelector('[data-id="{ov_id}"]');
              if (!el) return;
              if (el.parentNode !== document.body) document.body.appendChild(el); // вынесли под body

              // жёстко фиксируем к вьюпорту и нулим любые внешние влияния
              Object.assign(el.style, {{
                position: 'fixed',
                top: '0', left: '0', right: '0', bottom: '0',
                margin: '0',
                transform: 'none',          // на всякий случай
                WebkitTransform: 'none'
              }});
            }})();
            """)
            try:
                result = await fn(*args, **kwargs)
            finally:
                if auto_hide:
                    await asyncio.sleep(hide_delay_ms / 1000)
                    await hide()
            return result
        return _wrapped
    return _decorator

