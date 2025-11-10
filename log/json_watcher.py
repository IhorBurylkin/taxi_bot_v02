from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from log.log import log_info  # единая система логирования проекта

# Путь для дубля логов (изменено на logs_json.log)
JSON_LOG_PATH = os.path.expanduser("~/log/logs_json.log")
DEFAULT_DIR = ".nicegui"

# Ограничители вывода
MAX_PATHS_IN_LOG = 20       # максимум путей на раздел (+/~/−)
MAX_VALUE_LEN = 120         # максимум символов для значения

# Кэш последнего состояния: path -> ("json" | "text", payload)
_CONTENT_CACHE: Dict[str, Tuple[str, Any]] = {}


# -------------------- низкоуровневое дублирование в файл ----------------------
async def _ensure_json_log_dir() -> None:
    try:
        os.makedirs(os.path.dirname(JSON_LOG_PATH), exist_ok=True)
    except Exception as e:
        await log_info(f"[json_watch] не удалось создать каталог для логов: {e}", type_msg="warning")


async def _append_json_log_line(line: str) -> None:
    await _ensure_json_log_dir()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{ts} {line}\n"

    def _write() -> None:
        with open(JSON_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text)

    try:
        await asyncio.to_thread(_write)
    except Exception as e:
        await log_info(f"[json_watch] ошибка записи в logs_json.log: {e}", type_msg="warning")


def _short(path: str) -> str:
    try:
        return os.path.relpath(path, os.getcwd())
    except Exception:
        return path


async def _log_both(msg: str) -> None:
    await log_info(msg, type_msg="info")
    await _append_json_log_line(msg)


# ----------------------------- утилиты диффа ----------------------------------
def _mask_value(v: Any) -> str:
    """Маскировка длинных строк (защита от случайного логирования секретов)."""
    try:
        s = json.dumps(v, ensure_ascii=False)
    except Exception:
        s = repr(v)

    if isinstance(v, str) and len(v) >= 24:
        s = '"' + v[:3] + "…" + f"(len={len(v)})" + '"'
    if len(s) > MAX_VALUE_LEN:
        s = s[:MAX_VALUE_LEN] + "…"
    return s


def _flatten(obj: Any, base: str = "") -> Dict[str, Any]:
    """
    Превращает произвольный JSON (dict/list/скаляры) в плоский словарь:
    path -> value. Списки индексируются через [i].
    """
    out: Dict[str, Any] = {}

    def walk(x: Any, prefix: str) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                new_p = f"{prefix}.{k}" if prefix else str(k)
                walk(v, new_p)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                new_p = f"{prefix}[{i}]" if prefix else f"[{i}]"
                walk(v, new_p)
        else:
            out[prefix] = x

    walk(obj, base)
    return out


def _diff_json(old: Optional[Any], new: Optional[Any]) -> Tuple[List[str], List[str], List[Tuple[str, Any, Any]]]:
    """
    Возвращает (added_paths, removed_paths, changed_tuples(path, old, new)).
    Если old/new None, трактуем как создание/удаление всего дерева.
    """
    if old is None and new is None:
        return [], [], []

    if old is None:
        flat_new = _flatten(new)
        return sorted(flat_new.keys()), [], []
    if new is None:
        flat_old = _flatten(old)
        return [], sorted(flat_old.keys()), []

    flat_old = _flatten(old)
    flat_new = _flatten(new)

    keys_old = set(flat_old.keys())
    keys_new = set(flat_new.keys())

    added = sorted(keys_new - keys_old)
    removed = sorted(keys_old - keys_new)
    changed: List[Tuple[str, Any, Any]] = []

    for k in sorted(keys_old & keys_new):
        if flat_old[k] != flat_new[k]:
            changed.append((k, flat_old[k], flat_new[k]))

    return added, removed, changed


def _fmt_paths(paths: Iterable[str]) -> str:
    items = list(paths)
    more = ""
    if len(items) > MAX_PATHS_IN_LOG:
        more = f" (и ещё {len(items) - MAX_PATHS_IN_LOG})"
        items = items[:MAX_PATHS_IN_LOG]
    return ", ".join(items) + more


def _fmt_changed(changes: List[Tuple[str, Any, Any]]) -> str:
    more = ""
    if len(changes) > MAX_PATHS_IN_LOG:
        more = f" (и ещё {len(changes) - MAX_PATHS_IN_LOG})"
        changes = changes[:MAX_PATHS_IN_LOG]
    parts = [f"{p}: {_mask_value(o)} → {_mask_value(n)}" for p, o, n in changes]
    return "; ".join(parts) + more


# ----------------------------- загрузка/кэш -----------------------------------
async def _read_file(path: str) -> Optional[str]:
    def _read() -> Optional[str]:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    try:
        return await asyncio.to_thread(_read)
    except Exception:
        return None


async def _load_snapshot(path: str) -> Tuple[str, Any]:
    """
    Возвращает ("json", obj) либо ("text", str) если JSON не распарсился.
    """
    raw = await _read_file(path)
    if raw is None:
        return "text", ""

    with contextlib.suppress(Exception):
        return "json", json.loads(raw)

    # невалидный JSON -> логируем как текст
    return "text", raw


async def _prime_cache(root_dir: str) -> None:
    _CONTENT_CACHE.clear()
    for dirpath, _dirs, files in os.walk(root_dir):
        for name in files:
            if not name.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, name)
            kind, payload = await _load_snapshot(full)
            _CONTENT_CACHE[full] = (kind, payload)


# ------------------------------- логика событий --------------------------------
async def _log_created(path: str, new_kind: str, new_payload: Any) -> None:
    if new_kind == "json":
        added, _, _ = _diff_json(None, new_payload)
        details = f"+{len(added)} ключ(ей): " + _fmt_paths(added) if added else "создан пустой JSON"
    else:
        preview = _mask_value(new_payload or "")
        details = f"создан (невалидный JSON), текст: {preview}"
    await _log_both(f"[json_watch] создан: {_short(path)} | {details}")


async def _log_modified(path: str, old_kind: str, old_payload: Any, new_kind: str, new_payload: Any) -> None:
    if old_kind == "json" and new_kind == "json":
        added, removed, changed = _diff_json(old_payload, new_payload)
        parts: List[str] = []
        if added:
            parts.append(f"+{len(added)}: {_fmt_paths(added)}")
        if removed:
            parts.append(f"-{len(removed)}: {_fmt_paths(removed)}")
        if changed:
            parts.append(f"~{len(changed)}: {_fmt_changed(changed)}")
        details = " | ".join(parts) if parts else "без изменений полей (возможна перезапись теми же данными)"
    else:
        # смена формата или невалидный JSON — дадим текстовые превью
        old_pv = _mask_value(old_payload) if old_kind == "text" else "(JSON)"
        new_pv = _mask_value(new_payload) if new_kind == "text" else "(JSON)"
        details = f"формат: {old_kind} → {new_kind}; prev={old_pv}; new={new_pv}"

    await _log_both(f"[json_watch] изменён: {_short(path)} | {details}")


async def _log_deleted(path: str, old_kind: str, old_payload: Any) -> None:
    if old_kind == "json":
        _, removed, _ = _diff_json(old_payload, None)
        details = f"-{len(removed)} ключ(ей): " + _fmt_paths(removed) if removed else "удалён пустой JSON"
    else:
        preview = _mask_value(old_payload or "")
        details = f"удалён (невалидный JSON), прежний текст: {preview}"
    await _log_both(f"[json_watch] удалён: {_short(path)} | {details}")


# ----------------------------- реализация наблюдателей -------------------------
async def _watch_with_watchfiles(root_dir: str, stop_evt: asyncio.Event) -> None:
    try:
        from watchfiles import awatch, Change  # type: ignore
    except Exception as e:
        await log_info(f"[json_watch] watchfiles недоступен: {e} → fallback на поллинг", type_msg="warning")
        await _watch_with_polling(root_dir, stop_evt)
        return

    await log_info(f"[json_watch] запуск через watchfiles: {root_dir}", type_msg="info")
    await _prime_cache(root_dir)

    try:
        async for changes in awatch(root_dir, recursive=True):
            if stop_evt.is_set():
                break

            for change, path in changes:
                if not path.lower().endswith(".json"):
                    continue

                try:
                    if change == Change.added:
                        new_kind, new_payload = await _load_snapshot(path)
                        await _log_created(path, new_kind, new_payload)
                        _CONTENT_CACHE[path] = (new_kind, new_payload)

                    elif change == Change.modified:
                        old_kind, old_payload = _CONTENT_CACHE.get(path, ("text", ""))
                        new_kind, new_payload = await _load_snapshot(path)
                        await _log_modified(path, old_kind, old_payload, new_kind, new_payload)
                        _CONTENT_CACHE[path] = (new_kind, new_payload)

                    elif change == Change.deleted:
                        if path in _CONTENT_CACHE:
                            old_kind, old_payload = _CONTENT_CACHE.pop(path)
                            await _log_deleted(path, old_kind, old_payload)
                        else:
                            await _log_both(f"[json_watch] удалён: {_short(path)} | ранее не индексировался")
                except Exception as e:
                    await log_info(f"[json_watch] ошибка обработки события: {e}", type_msg="error")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await log_info(f"[json_watch] ошибка основного цикла watchfiles: {e}", type_msg="error")


async def _scan_json_files(root_dir: str) -> Dict[str, float]:
    res: Dict[str, float] = {}
    for dirpath, _dirs, files in os.walk(root_dir):
        for name in files:
            if not name.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, name)
            with contextlib.suppress(Exception):
                res[full] = os.path.getmtime(full)
    return res


async def _watch_with_polling(root_dir: str, stop_evt: asyncio.Event, period: float = 1.0) -> None:
    await log_info(f"[json_watch] запуск поллинга: {root_dir}", type_msg="info")
    await _prime_cache(root_dir)

    try:
        prev_mtime: Dict[str, float] = await _scan_json_files(root_dir)

        while not stop_evt.is_set():
            await asyncio.sleep(period)

            curr_mtime: Dict[str, float] = await _scan_json_files(root_dir)
            prev_paths = set(prev_mtime.keys())
            curr_paths = set(curr_mtime.keys())

            # добавленные
            for p in sorted(curr_paths - prev_paths):
                new_kind, new_payload = await _load_snapshot(p)
                await _log_created(p, new_kind, new_payload)
                _CONTENT_CACHE[p] = (new_kind, new_payload)

            # удалённые
            for p in sorted(prev_paths - curr_paths):
                if p in _CONTENT_CACHE:
                    old_kind, old_payload = _CONTENT_CACHE.pop(p)
                    await _log_deleted(p, old_kind, old_payload)
                else:
                    await _log_both(f"[json_watch] удалён: {_short(p)} | ранее не индексировался")

            # изменённые
            for p in sorted(curr_paths & prev_paths):
                try:
                    if curr_mtime[p] > prev_mtime[p]:
                        old_kind, old_payload = _CONTENT_CACHE.get(p, ("text", ""))
                        new_kind, new_payload = await _load_snapshot(p)
                        await _log_modified(p, old_kind, old_payload, new_kind, new_payload)
                        _CONTENT_CACHE[p] = (new_kind, new_payload)
                except Exception:
                    # На случай гонок — считаем как изменённый
                    old_kind, old_payload = _CONTENT_CACHE.get(p, ("text", ""))
                    new_kind, new_payload = await _load_snapshot(p)
                    await _log_modified(p, old_kind, old_payload, new_kind, new_payload)
                    _CONTENT_CACHE[p] = (new_kind, new_payload)

            prev_mtime = curr_mtime

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await log_info(f"[json_watch] ошибка цикла поллинга: {e}", type_msg="error")


# ------------------------------- публичный API --------------------------------
async def run_json_watch(nicegui_dir: str = DEFAULT_DIR, stop_event: Optional[asyncio.Event] = None) -> None:
    """
    Мониторинг .nicegui/*.json с детальными диффами.
    - Логи: await log_info(...) + дубль в ~/log/logs_json.log
    - Создание, изменение, удаление: показываем конкретные пути и значения (с ограничениями).
    - Корректное завершение по stop_event/отмене.
    """
    stop_event = stop_event or asyncio.Event()

    if not os.path.isdir(nicegui_dir):
        await log_info(f"[json_watch] каталог не найден: {nicegui_dir}", type_msg="warning")
        try:
            while not stop_event.is_set():
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass
        return

    await log_info("[json_watch] сервис мониторинга запущен", type_msg="info")

    try:
        await _watch_with_watchfiles(nicegui_dir, stop_event)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        await log_info(f"[json_watch] критическая ошибка: {e}", type_msg="error")
    finally:
        await log_info("[json_watch] сервис мониторинга остановлен", type_msg="info")
