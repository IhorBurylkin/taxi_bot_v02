import asyncio
import base64
import imghdr
import os

from nicegui import ui, app
from pathlib import Path
from web.web_notify import bind_current_client_for_user
from log.log import log_info
from db.db_utils import get_user_data
from config.config_utils import lang_dict
from aiogram.exceptions import TelegramAPIError


IMAGES_ROOT = Path('images')

DEFAULT_AVATAR_DATA_URL = (
  "data:image/svg+xml;utf8,"
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'>"
  "<rect width='128' height='128' rx='64' fill='%23D5D7DC'/>"
  "<circle cx='64' cy='54' r='25' fill='%239CA3AD'/>"
  "<path d='M32 112c0-19 15-34 34-34s34 15 34 34' fill='%23B6BCC6'/>"
  "</svg>"
)

def _digits(s: str) -> str: return ''.join(ch for ch in (s or '') if ch.isdigit())

async def _safe_js(code: str, *, timeout: float = 3.0) -> bool:
    try:
        await ui.run_javascript(code, timeout=timeout)
        return True
    except TimeoutError as te:
        await log_info(
            f"[js][TIMEOUT] {te!r} | code={code[:80]!r}",
            type_msg="warning",
        )
        return False
    except Exception as e:
        await log_info(
            f"[js][ОШИБКА] {e!r} | code={code[:80]!r}",
            type_msg="error",
        )
        return False

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


async def fetch_telegram_avatar(uid: int) -> str:
  """Возвращает data URL аватара пользователя через Bot API или стандартную иконку."""
  try:
    if not uid:
      await log_info(
        "[fetch_telegram_avatar] uid отсутствует, возвращаем заглушку",
        type_msg="warning",
        uid=uid,
      )
      return DEFAULT_AVATAR_DATA_URL

    # Ленивая загрузка экземпляра бота во избежание циклического импорта
    from bot_instance import bot as main_bot  # type: ignore

    if main_bot is None:
      await log_info(
        "[fetch_telegram_avatar] экземпляр бота недоступен, используем заглушку",
        type_msg="warning",
        uid=uid,
      )
      return DEFAULT_AVATAR_DATA_URL

    await log_info(
      f"[fetch_telegram_avatar] запрашиваю аватар для uid={uid}",
      type_msg="info",
      uid=uid,
    )

    chat = None
    try:
      chat = await main_bot.get_chat(uid)
    except TelegramAPIError as chat_error:
      await log_info(
        f"[fetch_telegram_avatar] get_chat не удался: {chat_error!r}",
        type_msg="warning",
        uid=uid,
      )
    except Exception as chat_unexpected:
      await log_info(
        f"[fetch_telegram_avatar][ОШИБКА get_chat] {chat_unexpected!r}",
        type_msg="error",
        uid=uid,
      )

    file_meta = None
    if chat and getattr(chat, "photo", None):
      photo = chat.photo
      file_id = (
        getattr(photo, "big_file_id", None)
        or getattr(photo, "small_file_id", None)
      )
      if file_id:
        try:
          file_meta = await main_bot.get_file(file_id)
          await log_info(
            "[fetch_telegram_avatar] найдено фото через get_chat",
            type_msg="info",
            uid=uid,
          )
        except TelegramAPIError as file_error:
          await log_info(
            f"[fetch_telegram_avatar] get_file не удался: {file_error!r}",
            type_msg="warning",
            uid=uid,
          )
        except Exception as unexpected_file_error:
          await log_info(
            f"[fetch_telegram_avatar][ОШИБКА get_file] {unexpected_file_error!r}",
            type_msg="error",
            uid=uid,
          )

    if file_meta is None:
      photos = await main_bot.get_user_profile_photos(user_id=uid, limit=1)
      if not photos.photos:
        await log_info(
          f"[fetch_telegram_avatar] фото для uid={uid} отсутствует, используем заглушку",
          type_msg="info",
          uid=uid,
        )
        return DEFAULT_AVATAR_DATA_URL

      # Берём максимально крупный вариант из первой фотографии
      sizes = photos.photos[0]
      size_obj = max(sizes, key=lambda item: item.file_size or 0)
      file_meta = await main_bot.get_file(size_obj.file_id)

    raw_data = await main_bot.download_file(file_meta.file_path)
    if hasattr(raw_data, "read"):
      raw_bytes = raw_data.read()
    else:
      raw_bytes = raw_data

    if not raw_bytes:
      await log_info(
        f"[fetch_telegram_avatar] не удалось скачать файл для uid={uid}, используем заглушку",
        type_msg="warning",
        uid=uid,
      )
      return DEFAULT_AVATAR_DATA_URL

    await log_info(
      f"[fetch_telegram_avatar] получено {len(raw_bytes)} байт для uid={uid}",
      type_msg="info",
      uid=uid,
    )

    detected_kind = imghdr.what(None, raw_bytes) or "jpeg"
    mime_subtype = "jpg" if detected_kind == "jpeg" else detected_kind

    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:image/{mime_subtype};base64,{encoded}"

  except TelegramAPIError as api_error:
    await log_info(
      f"[fetch_telegram_avatar][TelegramAPIError] {api_error!r} — используем заглушку",
      type_msg="error",
      uid=uid,
    )
    return DEFAULT_AVATAR_DATA_URL
  except Exception as avatar_error:
    await log_info(
      f"[fetch_telegram_avatar][ОШИБКА] {avatar_error!r} — используем заглушку",
      type_msg="error",
      uid=uid,
    )
    return DEFAULT_AVATAR_DATA_URL


async def get_user_data_uid_lang():
  """Возвращает (uid|None, user_lang, user_data|None).
  Никогда не возвращает одиночный None.
  """
  uid: int | None = None
  try:
    uid = await _get_uid()  # берём из Telegram.WebApp.initDataUnsafe.user.id
    default_lang = app.storage.user.get('lang') or 'en'

    if not uid:
      # вне Telegram Mini App (или ранний рендер) uid отсутствует — возвращаем корректную тройку
      ui.notify(lang_dict('notify_user_id_fail', default_lang), type='warning')
      await log_info(
        "[get_user_data_uid_lang] uid отсутствует (не TWA/ранний рендер)",
        type_msg="warning",
        uid=uid,
      )
      return None, default_lang, None

    await bind_current_client_for_user(int(uid))

    user_data = await get_user_data('users', uid)
    user_lang = (user_data or {}).get('language') or default_lang

    await log_info(
      f"[get_user_data_uid_lang] Получен язык пользователя: {user_lang!r}",
      type_msg="info",
      uid=uid,
    )
    return uid, user_lang, user_data

  except Exception as e:
    fallback_lang = app.storage.user.get('lang') or 'en'
    await log_info(
      f"[get_user_data_uid_lang][ОШИБКА] {e!r}",
      type_msg="error",
      uid=uid,
    )
    return None, fallback_lang, None