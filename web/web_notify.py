# web/web_notify.py
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set
import weakref

from nicegui import context, ui
from nicegui.client import Client

from log.log import log_info

# === Привязки и кэш клиентов ===
# user_id -> набор client.id (строки)
_TG_BIND: Dict[int, Set[str]] = {}
# client.id (строка) -> weakref на объект клиента NiceGUI
_CLIENTS: Dict[str, weakref.ReferenceType[Client]] = {}

# === Фича: буфер офлайн-уведомлений (in-memory) ===
# user_id -> список сообщений {text, level, position}
_PENDING: Dict[int, List[Dict[str, str]]] = {}
ENABLE_NOTIFY_BUFFER: bool = True  # можно выключить без изменения кода выше

# Где искать storage-файлы NiceGUI
NICEGUI_STORAGE_DIR: Path = Path(".nicegui")


def _resolve_client(cid: str) -> Optional[Client]:
    """Вернуть живой Client по id или None, если нет/удалён."""
    ref = _CLIENTS.get(cid)
    cl = ref() if ref else None
    if cl is None:
        return None
    # На некоторых версиях у клиента есть флаг deleted
    if getattr(cl, "deleted", False):
        return None
    return cl


async def _delete_client_storage(cid: str) -> bool:
    """Удалить .nicegui/storage-user-<cid>.json (best-effort)."""
    try:
        path = NICEGUI_STORAGE_DIR / f"storage-user-{cid}.json"
        if path.exists():
            path.unlink()
            await log_info(f"[notify.storage][DELETE] {path}", type_msg="info")
            return True
        await log_info(f"[notify.storage][MISS] {path}", type_msg="warning")
        return False
    except Exception as e:
        await log_info(f"[notify.storage][ОШИБКА] cid={cid} | {e!r}", type_msg="error")
        return False


async def _cleanup_client(cid: str, *, uid: Optional[int] = None, reason: str = "unknown") -> None:
    """Снять привязку, убрать из кэша, удалить storage-файл."""
    try:
        _CLIENTS.pop(cid, None)
        if uid is not None and uid in _TG_BIND:
            _TG_BIND[uid].discard(cid)
            if not _TG_BIND[uid]:
                _TG_BIND.pop(uid, None)
        await _delete_client_storage(cid)
        await log_info(f"[notify.cleanup] cid={cid} reason={reason}", type_msg="warning")
    except Exception as e:
        await log_info(f"[notify.cleanup][ОШИБКА] cid={cid} | {e!r}", type_msg="error")


async def bind_current_client_for_user(user_id: int) -> bool:
    """Привязать ТЕКУЩИЙ NiceGUI-клиент к Telegram user_id (вызывать внутри page())."""
    try:
        c = context.client
        if not c:
            await log_info(f"[notify.bind] Нет активного UI-контекста; uid={user_id}", type_msg="warning")
            return False

        cid = str(getattr(c, "id", ""))
        if not cid:
            await log_info(f"[notify.bind] Пустой client.id; uid={user_id}", type_msg="warning")
            return False

        _TG_BIND.setdefault(int(user_id), set()).add(cid)
        _CLIENTS[cid] = weakref.ref(c)

        await log_info(
            f"[notify.bind] Привязали client_id={cid} к uid={user_id}; всего_привязок={len(_TG_BIND[int(user_id)])}",
            type_msg="info",
        )

        # После привязки — попытка выслать отложенные уведомления
        await _flush_pending_notifications(int(user_id))
        return True

    except Exception as e:
        await log_info(f"[notify.bind][ОШИБКА] uid={user_id} | {e!r}", type_msg="error")
        return False


async def _flush_pending_notifications(user_id: int) -> int:
    """Отправить ранее накопленные сообщения пользователю, если есть."""
    try:
        bucket = _PENDING.get(int(user_id))
        if not bucket:
            return 0

        delivered = 0
        # Идём копией, чтобы можно было модифицировать bucket
        for msg in list(bucket):
            ok = await notify_user(
                user_id,
                msg.get("text", ""),
                level=msg.get("level", "info"),
                position=msg.get("position", "bottom"),
                queue_if_offline=False,  # важно: не класть обратно в очередь
            )
            if ok:
                bucket.remove(msg)
                delivered += 1

        if not bucket:
            _PENDING.pop(int(user_id), None)

        await log_info(
            f"[notify.flush] uid={user_id} delivered={delivered} left={len(_PENDING.get(int(user_id), []))}",
            type_msg="info",
        )
        return delivered

    except Exception as e:
        await log_info(f"[notify.flush][ОШИБКА] uid={user_id} | {e!r}", type_msg="error")
        return 0


async def notify_user(
    user_id: int,
    text: str,
    *,
    level: str = "info",
    position: str = "bottom",
    queue_if_offline: bool = True,
) -> bool:
    """Показать ui.notify у нужного user_id. Если клиентов нет — опционально положить в очередь.

    Возвращает True, если отправили хотя бы одному живому клиенту.
    """
    try:
        target_ids = [str(x) for x in _TG_BIND.get(int(user_id), set())]
        await log_info(
            f"[notify.send] uid={user_id} привязанных_клиентов={len(target_ids)}",
            type_msg="info",
        )
        if not target_ids:
            if ENABLE_NOTIFY_BUFFER and queue_if_offline:
                _PENDING.setdefault(int(user_id), []).append(
                    {"text": text, "level": level, "position": position}
                )
                await log_info(f"[notify.send][QUEUE] uid={user_id}", type_msg="warning")
            else:
                await log_info(f"[notify.send][SKIP] нет клиентов uid={user_id}", type_msg="warning")
            return False

        sent = False
        # Перебираем текущую копию ID (могут отвалиться во время рассылки)
        for cid in list(target_ids):
            try:
                cl = _resolve_client(cid)
                if cl is None:
                    await _cleanup_client(cid, uid=int(user_id), reason="dead_or_missing")
                    continue

                with cl:
                    ui.notify(text, position=position, type=level)
                sent = True
                await log_info(
                    f"[notify.send] Показали notify client_id={cid}; uid={user_id}",
                    type_msg="info",
                )

            except Exception as e:
                await log_info(
                    f"[notify.send][ОШИБКА] client_id={cid} uid={user_id} | {e!r}",
                    type_msg="error",
                )

        # Никому не отправили — положим в очередь (если разрешено)
        if not sent and ENABLE_NOTIFY_BUFFER and queue_if_offline:
            _PENDING.setdefault(int(user_id), []).append(
                {"text": text, "level": level, "position": position}
            )
            await log_info(f"[notify.send][QUEUE_AFTER] uid={user_id}", type_msg="warning")

        return sent

    except Exception as e:
        await log_info(f"[notify.send][ОШИБКА-ОБЩАЯ] uid={user_id} | {e!r}", type_msg="error")
        return False


async def prune_dead_clients() -> int:
    """Пройтись по кэшу клиентов и убрать умершие (с чисткой storage)."""
    try:
        removed = 0
        for cid in list(_CLIENTS.keys()):
            cl = _resolve_client(cid)
            if cl is None:
                # uid неизвестен; вычистим из всех списков
                for uid, ids in list(_TG_BIND.items()):
                    if cid in ids:
                        ids.discard(cid)
                        if not ids:
                            _TG_BIND.pop(uid, None)
                await _cleanup_client(cid, reason="prune")
                removed += 1
        await log_info(f"[notify.prune] удалено={removed}", type_msg="info")
        return removed
    except Exception as e:
        await log_info(f"[notify.prune][ОШИБКА] {e!r}", type_msg="error")
        return 0
