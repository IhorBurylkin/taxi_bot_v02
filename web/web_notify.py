# web/notify.py
from __future__ import annotations
from typing import Dict, Set
import weakref

from nicegui import ui, context
from log.log import log_info

# user_id -> набор client.id (строки)
_TG_BIND: Dict[int, Set[str]] = {}
# client.id (строка) -> weakref на объект клиента NiceGUI
_CLIENTS: Dict[str, weakref.ReferenceType] = {}


async def bind_current_client_for_user(user_id: int) -> bool:
    """Привязать ТЕКУЩИЙ NiceGUI-клиент к Telegram user_id (вызывать внутри page())."""
    try:
        c = context.client
        if not c:
            await log_info(f"[notify.bind] Нет активного UI-контекста; uid={user_id}", type_msg="warning")
            return False

        cid = str(getattr(c, 'id', ''))
        if not cid:
            await log_info(f"[notify.bind] Пустой client.id; uid={user_id}", type_msg="warning")
            return False

        _TG_BIND.setdefault(int(user_id), set()).add(cid)
        _CLIENTS[cid] = weakref.ref(c)

        await log_info(
            f"[notify.bind] Привязали client_id={cid} к uid={user_id}; всего_привязок={len(_TG_BIND[int(user_id)])}",
            type_msg="info",
        )
        return True

    except Exception as e:
        await log_info(f"[notify.bind][ОШИБКА] uid={user_id} | {e!r}", type_msg="error")
        return False


async def notify_user(user_id: int, text: str, level: str = "info", position: str = "center") -> bool:
    """Показать ui.notify ТОЛЬКО у нужного Telegram user_id (без глобального поиска клиентов)."""
    try:
        target_ids = [str(x) for x in _TG_BIND.get(int(user_id), set())]
        await log_info(f"[notify.send] uid={user_id} привязанных_клиентов={len(target_ids)}", type_msg="info")
        if not target_ids:
            await log_info(f"[notify.send] Нет активных привязок для uid={user_id}", type_msg="warning")
            return False

        sent = False
        for cid in list(target_ids):
            try:
                ref = _CLIENTS.get(cid)
                cl = ref() if ref else None
                if not cl:
                    # клиент умер/отключился — подчистим
                    _TG_BIND[int(user_id)].discard(cid)
                    _CLIENTS.pop(cid, None)
                    await log_info(
                        f"[notify.send] client_id={cid} недоступен → удалили привязку; uid={user_id}",
                        type_msg="warning",
                    )
                    continue

                with cl:
                    ui.notify(text, position=position, type=level)
                sent = True
                await log_info(f"[notify.send] Показали notify client_id={cid}; uid={user_id}", type_msg="info")

            except Exception as e:
                await log_info(f"[notify.send][ОШИБКА] client_id={cid} uid={user_id} | {e!r}", type_msg="error")

        return sent

    except Exception as e:
        await log_info(f"[notify.send][ОШИБКА-ОБЩАЯ] uid={user_id} | {e!r}", type_msg="error")
        return False
