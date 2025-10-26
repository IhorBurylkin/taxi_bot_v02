import asyncio
import os

from nicegui import ui, app
from pathlib import Path
from web.web_notify import bind_current_client_for_user
from log.log import log_info
from db.db_utils import get_user_data
from config.config_utils import lang_dict


IMAGES_ROOT = Path('images')

def _digits(s: str) -> str: return ''.join(ch for ch in (s or '') if ch.isdigit())

async def _get_uid() -> int | None:
    js = """
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

    js = """
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