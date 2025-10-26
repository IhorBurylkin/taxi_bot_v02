import asyncio
import os
import json
import re

from nicegui import ui, app, context
from nicegui.events import KeyEventArguments
from pathlib import Path
from web.web_notify import bind_current_client_for_user
from log.log import log_info
from db.db_utils import get_user_data
from config.config_utils import lang_dict
from typing import Callable
from urllib.parse import quote as _url_quote


IMAGES_ROOT = Path('images')

def _digits(s: str) -> str: return ''.join(ch for ch in (s or '') if ch.isdigit())

async def _get_uid() -> int | None:
    js = r"""
    new Promise(async (resolve) => {
      const wait = (ms)=>new Promise(r=>setTimeout(r,ms));
      for (let i=0;i<80;i++){ // ~1.2s
        try{
          const id = window?.Telegram?.WebApp?.initDataUnsafe?.user?.id;
        if (id){ resolve(String(id)); return; }
        }catch(_){}
        await wait(15);
      }
      try{
        const ls = localStorage.getItem('tg_user_id');
        resolve(ls ? String(ls) : null);
      }catch(_){ resolve(null); }
    })
    """
    uid = await ui.run_javascript(js, timeout=1.6)
    try:
        return int(uid) if uid else None
    except:
        return None

async def _save_upload(uid: int, e, kind: str, progress=None) -> str:
    """Сохранение аплоада: поддерживает NiceGUI v2 (e.name/e.content) и v3 (e.file)."""
    IMAGES_ROOT.mkdir(exist_ok=True)
    user_dir = IMAGES_ROOT / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)

    # --- v3 API ---
    if hasattr(e, 'file') and e.file is not None:
        fobj = e.file                              # FileUpload
        name = fobj.name or f'{kind}.bin'
        ext = os.path.splitext(name)[1].lower()
        if ext not in {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}:
            raise ValueError('Недопустимый тип файла')
        path = user_dir / f'{kind}{ext}'

        # потоковая запись с прогрессом
        get_sz = getattr(fobj, 'size', None)
        total = get_sz() if callable(get_sz) else (int(get_sz) if get_sz is not None else None)

        written = 0
        with open(path, 'wb') as dst:
            async for chunk in fobj.iterate():     # 3.x: побайтовый итератор
                dst.write(chunk)
                if total and progress is not None:
                    written += len(chunk)
                    progress.value = written / total
                    await asyncio.sleep(0)
        return str(path)

    # --- v2 API (fallback) ---
    name = getattr(e, 'name', f'{kind}.bin')
    ext = os.path.splitext(name)[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}:
        raise ValueError('Недопустимый тип файла')
    path = user_dir / f'{kind}{ext}'

    src = getattr(e, 'content', None)
    if src is None:
        raise ValueError('Пустой контент файла')

    try:
        src.seek(0, os.SEEK_END); total = src.tell(); src.seek(0)
    except Exception:
        total = None

    copied = 0
    CHUNK = 256 * 1024
    with open(path, 'wb') as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if total and progress is not None:
                copied += len(chunk)
                progress.value = copied / total
                await asyncio.sleep(0)
    try:
        src.close()
    except Exception:
        pass
    return str(path)


def bind_enter_action(src, dst=None, close=False):
    src_id = getattr(src, 'id', None)
    dst_id = getattr(dst, 'id', None)
    if not src_id:
        return

    js = r"""
(() => {
  const SRC_ID = __SRC__;
  const DST_ID = __DST__;
  const CLOSE  = __CLOSE__;

  const rootEl = (id) => {
    try { return (window.getHtmlElement ? getHtmlElement(id) : document.getElementById(id)) || null; }
    catch(_){ return null; }
  };
  const pickInput = (id) => {
    const r = rootEl(id);
    if (!r) return null;
    return r.querySelector?.('input,textarea,[contenteditable="true"]') || r;
  };
  const isEnterLike = (ev) => {
    const k=(ev.key||'').toLowerCase(), c=(ev.code||'').toLowerCase();
    return ev.keyCode===13 || k==='enter'||k==='return'||k==='go'||k==='done'||k==='next'||k==='send'||k==='search' || c==='enter'||c==='numpadenter';
  };
  const emit = (msg, extra={}) => { try{ emitEvent('enter_dbg', Object.assign({msg, src:SRC_ID, dst:DST_ID}, extra)); }catch{} };

  let boundNode = null;

  const go = () => {
    if (CLOSE || DST_ID == null) {
      document.activeElement?.blur?.(); emit('do-blur'); return;
    }
    const d = pickInput(DST_ID);
    if (d) {
      d.focus();
      setTimeout(()=>d.select?.(),0);
      d.scrollIntoView?.({block:'center', inline:'nearest'});
      emit('focus-next-ok');
    } else {
      document.activeElement?.blur?.(); emit('next-missing->blur');
    }
  };

  const ensureImeForm = (input) => {
    try {
      const formId = 'ime_form_' + SRC_ID;
      let form = document.getElementById(formId);
      if (!form) {
        form = document.createElement('form');
        form.id = formId;
        form.style.position='fixed';
        form.style.width='0'; form.style.height='0';
        form.style.opacity='0'; form.style.overflow='hidden';
        form.tabIndex = -1;
        form.addEventListener('submit', (ev) => { ev.preventDefault(); ev.stopPropagation(); go(); }, true);
        const sub = document.createElement('input');
        sub.type='submit'; sub.tabIndex=-1;
        sub.style.position='absolute'; sub.style.left='-9999px'; sub.style.width='1px'; sub.style.height='1px';
        form.appendChild(sub);
        document.body.appendChild(form);
      }
      input.setAttribute('form', formId);
      input.setAttribute('enterkeyhint', (CLOSE || DST_ID == null) ? 'done' : 'next'); // подсказка клавиатуре
    } catch {}
  };

  const attach = () => {
    const srcInput = pickInput(SRC_ID);
    if (!srcInput) { emit('no-inner-yet'); return; }
    if (srcInput === boundNode) return;

    const onKey = (ev) => { if (!isEnterLike(ev)) return;
      ev.preventDefault(); ev.stopPropagation(); ev.stopImmediatePropagation();
      emit('key',{type:ev.type,key:ev.key,code:ev.code,kC:ev.keyCode}); go();
    };
    const onBefore = (ev) => {
      const t=ev.inputType||'';
      if (t==='insertLineBreak'||t==='insertParagraph'){
        ev.preventDefault(); ev.stopPropagation(); ev.stopImmediatePropagation();
        emit('beforeinput',{t}); go();
      }
    };

    // очистка старых слушателей
    if (boundNode) {
      boundNode.removeEventListener('keydown', onKey, true);
      boundNode.removeEventListener('beforeinput', onBefore, true);
    }
    srcInput.addEventListener('keydown', onKey, true);
    srcInput.addEventListener('beforeinput', onBefore, true);

    ensureImeForm(srcInput);

    boundNode = srcInput;
    emit('attached', {hasDst: !!DST_ID});
  };

  const tryAttach = () => {
    const root = rootEl(SRC_ID);
    if (root) {
      if (!root.__enter_obs__) {
        const mo = new MutationObserver(attach);
        mo.observe(root, {subtree:true, childList:true});
        root.__enter_obs__ = mo;
      }
      attach();
      return true;
    }
    return false;
  };

  if (!tryAttach()) {
    const key = '__enter_doc_obs_' + SRC_ID;
    if (!document[key]) {
      const moDoc = new MutationObserver((_muts) => {
        if (tryAttach()) { moDoc.disconnect(); document[key] = null; emit('root-appeared'); }
      });
      moDoc.observe(document.documentElement, {subtree:true, childList:true});
      document[key] = moDoc;
      emit('watching-doc');
    }
  }
})();
"""
    js = js.replace('__SRC__', str(src_id))
    js = js.replace('__DST__', 'null' if dst_id is None else str(dst_id))
    js = js.replace('__CLOSE__', 'true' if (close or dst_id is None) else 'false')
    ui.run_javascript(js)

async def get_user_data_uid_lang():
    """Возвращает (uid|None, user_lang, user_data|None).
    Никогда не возвращает одиночный None.
    """
    try:
        uid = await _get_uid()  # берём из Telegram.WebApp.initDataUnsafe.user.id
        # безопасный язык по умолчанию (если в БД/инициализации нет языка)
        default_lang = app.storage.user.get('lang') or 'en'

        if not uid:
            # вне Telegram Mini App (или ранний рендер) uid отсутствует — возвращаем корректную тройку
            ui.notify(lang_dict('notify_user_id_fail', default_lang), type='warning')
            await log_info("[get_user_data_uid_lang] uid отсутствует (не TWA/ранний рендер)", type_msg="warning")
            return None, default_lang, None

        await bind_current_client_for_user(int(uid))

        user_data = await get_user_data('users', uid)
        user_lang = (user_data or {}).get('language') or default_lang

        await log_info(f"[get_user_data_uid_lang] Получен язык пользователя: {user_lang!r}", type_msg="info")
        return uid, user_lang, user_data

    except Exception as e:
        # даже при ошибке возвращаем тройку
        fallback_lang = app.storage.user.get('lang') or 'en'
        await log_info(f"[get_user_data_uid_lang][ОШИБКА] {e!r}", type_msg="error")
        return None, fallback_lang, None

    
# --- Splash screen utilities for Spar-Taxi (NiceGUI) -------------------------

def _bind_splash_log_handlers() -> None:
    if getattr(_bind_splash_log_handlers, '_bound', False):
        return
    _bind_splash_log_handlers._bound = True

    async def on_dbg(e):
        # e.args — словарь из JS
        await log_info(f"[SPLASH-DBG] {e.args}", type_msg="debug")

    async def on_err(e):
        await log_info(f"[SPLASH-ERR] {e.args}", type_msg="error")

    ui.on('splash_dbg', on_dbg)
    ui.on('splash_err', on_err)

def _spar_taxi_default_svg() -> str:
    """Адаптивный SVG, заполняет контейнер; текст с подсветкой, чтобы не «терялся»."""
    return r'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 1024 640"
     width="100%" height="100%"
     preserveAspectRatio="xMidYMid slice">
  <defs>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f1013"/>
      <stop offset="100%" stop-color="#151923"/>
    </linearGradient>

    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="6" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>

    <!-- мягкая подсветка для подписей -->
    <filter id="txt" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="2" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>

    <style><![CDATA[
      :root{
        --wave1:20s; --wave2:28s; --pulse:4s; --dots:1.6s;
        /* адаптивные размеры шрифтов */
        --brand: clamp(22px, 8vw, 46px);
        --tag:   clamp(12px, 4.2vw, 20px);
        --load:  clamp(12px, 3.8vw, 18px);
      }
      @keyframes slideL{from{transform:translateX(0)}to{transform:translateX(-256px)}}
      @keyframes slideR{from{transform:translateX(-256px)}to{transform:translateX(0)}}
      @keyframes pulse {0%,100%{opacity:.75}50%{opacity:1}}
      @keyframes blink {0%,100%{opacity:0}50%{opacity:1}}

      .w1{animation:slideL var(--wave1) linear infinite}
      .w2{animation:slideR var(--wave2) linear infinite}
      .fade{animation:pulse var(--pulse) ease-in-out infinite}
      .dot{animation:blink var(--dots) ease-in-out infinite}
      .dot2{animation-delay:.2s}.dot3{animation-delay:.4s}

      .brand{font:700 var(--brand)/1 "Inter","Segoe UI",Roboto,Arial,sans-serif; fill:#fff}
      .tagline{font:500 var(--tag)/1.35 "Inter","Segoe UI",Roboto,Arial,sans-serif; fill:#f2f6ff; opacity:.95}
      .loader{font:600 var(--load)/1 "Inter","Segoe UI",Roboto,Arial,sans-serif; fill:#eaf3ff}
    ]]></style>


    <symbol id="tile" viewBox="0 0 256 160">
      <rect width="256" height="160" fill="#0057B7"/>
      <path d="M0,90 C64,70 192,110 256,90 V160 H0 Z" fill="#FFD500"/>
    </symbol>
  </defs>

  <!-- фон -->
  <rect width="100%" height="100%" fill="url(#bgGrad)"/>

  <!-- волны -->
  <g opacity="0.25" transform="translate(0,330)">
    <g class="w1">
      <use href="#tile" x="0" y="-160"/><use href="#tile" x="256" y="-160"/>
      <use href="#tile" x="512" y="-160"/><use href="#tile" x="768" y="-160"/><use href="#tile" x="1024" y="-160"/>
    </g>
    <g class="w2" transform="translate(-128,30) scale(1.05)">
      <use href="#tile" x="0" y="-160"/><use href="#tile" x="256" y="-160"/>
      <use href="#tile" x="512" y="-160"/><use href="#tile" x="768" y="-160"/><use href="#tile" x="1024" y="-160"/>
    </g>
  </g>

  <!-- ВЕСЬ контент строго по центру «safe area», чтобы его не срезало при cover -->
  <g transform="translate(512,300)">
    <g filter="url(#glow)" transform="translate(-190,-70)">
      <rect x="0" y="0" rx="14" ry="14" width="380" height="110" fill="#1b1e27" opacity="0.75"/>
      <rect x="10" y="10" rx="10" ry="10" width="360" height="90" fill="#FFD500" />
      <g transform="translate(22,27) scale(0.9)">
        <rect x="0" y="0" width="60" height="36" rx="4" fill="#1b1e27"/>
        <rect x="70" y="0" width="60" height="36" rx="4" fill="#1b1e27"/>
        <rect x="140" y="0" width="60" height="36" rx="4" fill="#1b1e27"/>
        <rect x="210" y="0" width="60" height="36" rx="4" fill="#1b1e27"/>
      </g>
      <text x="190" y="75" text-anchor="middle" class="brand fade" filter="url(#txt)">SPAR-TAXI</text>
    </g>

    <text x="0" y="98" text-anchor="middle" class="brand" filter="url(#txt)">für Ukrainer</text>
    <text x="0" y="132" text-anchor="middle" class="tagline" filter="url(#txt)">
      <tspan x="0" dy="0">Таксі, що долає від серця до серця</tspan>
      <tspan x="0" dy="1.4em">Schleswig-Holstein &amp; Hamburg</tspan>
    </text>

    <g transform="translate(0,168)">
      <text x="-36" y="20" text-anchor="end" class="loader" filter="url(#txt)">Завантаження</text>
      <circle class="dot"  cx="0"  cy="-6" r="4" fill="#eaf3ff"/>
      <circle class="dot dot2" cx="18" cy="-6" r="4" fill="#eaf3ff"/>
      <circle class="dot dot3" cx="36" cy="-6" r="4" fill="#eaf3ff"/>
    </g>
  </g>
</svg>'''


def get_spar_taxi_splash_svg(path: str | None = None) -> str:
    """
    Возвращает SVG сплэша. Если передан путь и файл существует — читаем его;
    иначе отдаём встроенный дефолт.
    """
    try:
        if path:
            p = Path(path)
            if p.exists():
                return p.read_text(encoding='utf-8')
    except Exception:
        pass
    return _spar_taxi_default_svg()


def show_startup_splash_img_min(svg_markup: str | None, *, fade_ms: int = 500, z_index: int = 2_147_483_000):
    """Полноэкранный сплэш: <img src='data:svg'> + object-fit:cover, без JS.
    Возвращает (overlay, async hide_fn)."""
    svg = svg_markup or _spar_taxi_default_svg()

    overlay = ui.element('div').style(
        'position:fixed;inset:0;z-index:%d;display:block;overflow:hidden;'
        'background:var(--tg-theme-bg-color,#0f1013);'
        'transition:opacity %dms ease;' % (z_index, fade_ms)
    )

    with overlay:
        data_url = 'data:image/svg+xml;charset=utf-8,' + _url_quote(svg, safe='')
        ui.element('img').props(f'src={data_url}').style(
            # растягиваем картинку на весь вьюпорт без искажений, лишнее обрезаем
            'position:absolute;inset:0;display:block;vertical-align:top;'
            'width:100%;height:100vh;height:100svh;height:100dvh;'
            'object-fit:cover;object-position:center;'
            'pointer-events:none;user-select:none;-webkit-user-drag:none;'
        )

    async def hide():
        try:
            overlay.style('opacity:0;pointer-events:none;')
            await asyncio.sleep(fade_ms / 1000 + 0.05)
            overlay.delete()
        except Exception:
            pass

    return overlay, hide
