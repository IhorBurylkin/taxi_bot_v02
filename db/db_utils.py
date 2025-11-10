from db.db_table_init import get_connection, release_connection
from log.log import log_info
from typing import Optional, Dict, List, Union, Tuple, Any, Iterable, Literal
import json
from datetime import datetime, date, time, timezone
from decimal import Decimal
from enum import Enum
from config.config import get_settings

def _jsonable(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return str(obj)

# Формат JSON-колонки support_requests.messages:
# {
#   "items": [
#     {
#       "id": "uuid",
#       "ts": "ISO8601",
#       "author": "user" | "admin",
#       "text": "...",
#       "attachments": [{"kind": "document" | "photo", ...}],
#       "meta": {"source": "telegram|web|admin_chat", ...}
#     },
#     ...
#   ],
#   "cursors": {"user_last_read": "ISO8601" | null, "admin_last_read": "ISO8601" | null},
#   "meta": {"updated_at": "ISO8601", "last_author": "user|admin"}
# }
# Такая структура облегчает сортировку по временной метке и вычисление непрочитанных сообщений.


def _normalize_support_thread(raw: Any) -> Dict[str, Any]:
    """Возвращает корректный словарь диалога, даже если в БД лежат повреждённые данные."""
    thread: Dict[str, Any] = {"items": [], "cursors": {}, "meta": {}}

    # Преобразуем строковое JSON-представление, если колонка хранится как TEXT
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return thread

    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, list):
            thread["items"] = [item for item in items if isinstance(item, dict)]
        cursors = raw.get("cursors")
        if isinstance(cursors, dict):
            thread["cursors"] = {
                "user_last_read": cursors.get("user_last_read"),
                "admin_last_read": cursors.get("admin_last_read"),
            }
        meta = raw.get("meta")
        if isinstance(meta, dict):
            thread["meta"] = meta
    return thread


def _parse_support_ts(value: Any) -> datetime | None:
    """Безопасно парсит timestamp из ISO-строки."""
    if not value or not isinstance(value, str):
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _latest_timestamp(items: List[Dict[str, Any]], *, author: str | None = None) -> str | None:
    """Возвращает ISO8601 последнего сообщения (опц. по автору)."""
    candidates: List[str] = []
    for item in items:
        if author is not None and item.get("author") != author:
            continue
        ts = item.get("ts")
        if isinstance(ts, str):
            candidates.append(ts)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda x: _parse_support_ts(x) or datetime.min.replace(tzinfo=timezone.utc))
    except Exception:
        return max(candidates)


def _sorted_support_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Возвращает список сообщений, отсортированный по временной метке."""
    return sorted(
        items,
        key=lambda item: _parse_support_ts(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc),
    )


async def _save_support_thread(connection, user_id: int, thread: Dict[str, Any]) -> None:
    """Сохраняет JSON-структуру диалога в таблицу support_requests."""
    payload = json.dumps(thread, ensure_ascii=False)
    await connection.execute(
        (
            'INSERT INTO "support_requests" (user_id, messages) VALUES ($1, $2::jsonb) '
            'ON CONFLICT (user_id) DO UPDATE SET messages = EXCLUDED.messages;'
        ),
        user_id,
        payload,
    )


async def get_support_thread(user_id: int) -> Dict[str, Any]:
    """Возвращает историю обращений пользователя в техподдержку."""
    connection = None
    try:
        connection = await get_connection()
        row = await connection.fetchrow('SELECT messages FROM "support_requests" WHERE user_id = $1;', user_id)
        payload = dict(row).get("messages") if row else None
        thread = _normalize_support_thread(payload)
        thread["items"] = _sorted_support_items(thread.get("items", []))
        return thread
    except Exception as error:
        await log_info(
            f"get_support_thread: ошибка чтения диалога → {error}",
            type_msg="error",
            user_id=user_id,
        )
        return {"items": [], "cursors": {}, "meta": {}}
    finally:
        if connection:
            await release_connection(connection)


async def append_support_message(
    user_id: int,
    entry: Dict[str, Any],
    *,
    author: Literal["user", "admin"],
) -> Dict[str, Any] | None:
    """Добавляет сообщение в историю техподдержки с учётом блокировок."""
    connection = None
    try:
        connection = await get_connection()
        async with connection.transaction():
            row = await connection.fetchrow(
                'SELECT messages FROM "support_requests" WHERE user_id = $1 FOR UPDATE;',
                user_id,
            )
            payload = dict(row).get("messages") if row else None
            thread = _normalize_support_thread(payload)
            items: List[Dict[str, Any]] = thread.get("items", [])

            safe_entry = dict(entry or {})
            safe_entry.setdefault("author", author)
            if not safe_entry.get("ts"):
                safe_entry["ts"] = datetime.now(timezone.utc).isoformat()

            items.append(safe_entry)
            thread["items"] = _sorted_support_items(items)

            cursors = thread.setdefault("cursors", {})
            cursors_key = "user_last_read" if author == "user" else "admin_last_read"
            cursors[cursors_key] = safe_entry.get("ts")

            meta = thread.setdefault("meta", {})
            meta["updated_at"] = safe_entry.get("ts")
            meta["last_author"] = author

            await _save_support_thread(connection, user_id, thread)

        await log_info(
            "append_support_message: сообщение сохранено", type_msg="info", user_id=user_id
        )
        return thread
    except Exception as error:
        await log_info(
            f"append_support_message: ошибка записи сообщения → {error}",
            type_msg="error",
            user_id=user_id,
        )
        return None
    finally:
        if connection:
            await release_connection(connection)


async def mark_support_thread_read(
    user_id: int,
    role: Literal["user", "admin"],
    ts: str | None = None,
) -> bool:
    """Обновляет курсор прочитанных сообщений для пользователя или администратора."""
    connection = None
    try:
        connection = await get_connection()
        async with connection.transaction():
            row = await connection.fetchrow(
                'SELECT messages FROM "support_requests" WHERE user_id = $1 FOR UPDATE;',
                user_id,
            )
            payload = dict(row).get("messages") if row else None
            if payload is None:
                return False

            thread = _normalize_support_thread(payload)
            items = _sorted_support_items(thread.get("items", []))
            thread["items"] = items

            cursor_key = "user_last_read" if role == "user" else "admin_last_read"
            target_ts = ts or _latest_timestamp(items)
            if not target_ts:
                return False

            prev_dt = _parse_support_ts(thread.get("cursors", {}).get(cursor_key))
            next_dt = _parse_support_ts(target_ts)
            if prev_dt and next_dt and next_dt <= prev_dt:
                return True

            thread.setdefault("cursors", {})[cursor_key] = target_ts
            thread.setdefault("meta", {})["last_read_role"] = role

            await _save_support_thread(connection, user_id, thread)

        await log_info(
            f"mark_support_thread_read: курсор для {role} обновлён", type_msg="info", user_id=user_id
        )
        return True
    except Exception as error:
        await log_info(
            f"mark_support_thread_read: ошибка обновления курсора → {error}",
            type_msg="error",
            user_id=user_id,
        )
        return False
    finally:
        if connection:
            await release_connection(connection)

async def user_exists(user_id: int) -> bool:
    """
    Asynchronous function to check if user_id exists in the PostgreSQL database.
    
    Parameters:
      user_id: int - identifier of the user to find.
      
    Returns:
      True if the user is found, False if the user doesn't exist or an error occurred.
    """
    connection = None
    try:
        connection = await get_connection()
        # Execute a query to check if the user exists.
        # Here the table is called "chat_ids", and it's assumed to have a "user_id" column.
        query = "SELECT 1 FROM users WHERE user_id = $1 LIMIT 1;"
        result = await connection.fetchval(query, user_id)
        
        await log_info(
            f"Проверка user_id {user_id} в таблице users завершена успешно.",
            type_msg="info",
            user_id=user_id,
        )
        
        # If result is not None, the user was found.
        return True if result else False
    except Exception as e:
        await log_info(
            f"Ошибка проверки пользователя {user_id}: {e}",
            type_msg="error",
            user_id=user_id,
        )
        return False
    finally:
        if connection:
            await release_connection(connection)

async def insert_into_table(table_name: str, data: dict, return_order_id: bool = False) -> bool:
    """
    Асинхронная функция для записи данных в указанную таблицу PostgreSQL.
    
    Параметры:
      table_name: str - название таблицы, в которую записываем.
      data: dict - словарь {колонка: значение}.
    
    Возвращает:
      - Если order_id == True → вернёт сгенерированный PK из колонки order_id (int или None при ошибке).
      - Если order_id == False → вернёт True/False по результату вставки.
    """
    connection = await get_connection()
    try:
        cols = ", ".join(data.keys())
        vals = ", ".join([f"${i}" for i in range(1, len(data) + 1)])

        if return_order_id:
            # вернуть значение PK из колонки order_id
            q = f"INSERT INTO {table_name} ({cols}) VALUES ({vals}) RETURNING order_id"
            try:
                return await connection.fetchval(q, *data.values())  # -> int | None
            except Exception as e:
                await log_info(f"insert_into_table RETURNING order_id failed: {e}", type_msg="error")
                return None
        else:
            # обычная вставка без возврата
            q = f"INSERT INTO {table_name} ({cols}) VALUES ({vals})"
            try:
                await connection.execute(q, *data.values())
                return True
            except Exception as e:
                await log_info(f"insert_into_table failed: {e}", type_msg="error")
                return False
    finally:
        await release_connection(connection)

async def update_table(table_name: str, user_id: int, updates: dict, data_order_id: bool = False) -> bool:
    """
    Асинхронная функция для обновления значений в указанной таблице PostgreSQL.

    Args:
        table_name (str): Название таблицы.
        user_id (int): Уникальный идентификатор пользователя (по колонке user_id).
        updates (dict): Словарь {колонка: новое_значение}.

    Returns:
        bool: True, если обновление прошло успешно, False в случае ошибки.
    """
    connection = None
    try:
        connection = await get_connection()

        if data_order_id:
            set_expr = ", ".join([f'"{col}" = ${i+1}' for i, col in enumerate(updates.keys())])
            values = list(updates.values())

            # добавляем order_id в параметры
            query = f'UPDATE "{table_name}" SET {set_expr} WHERE order_id = ${len(values) + 1};'
            values.append(user_id)

            await connection.execute(query, *values)

            await log_info(
                f"Обновлены колонки {list(updates.keys())} в таблице {table_name} для order_id={user_id}",
                type_msg="info",
                user_id=user_id,
            )
            return True
        else:
            # формируем SET часть запроса
            set_expr = ", ".join([f'"{col}" = ${i+1}' for i, col in enumerate(updates.keys())])
            values = list(updates.values())

            # добавляем user_id в параметры
            query = f'UPDATE "{table_name}" SET {set_expr} WHERE user_id = ${len(values) + 1};'
            values.append(user_id)

            await connection.execute(query, *values)

            await log_info(
                f"Обновлены колонки {list(updates.keys())} в таблице {table_name} для user_id={user_id}",
                type_msg="info",
                user_id=user_id,
            )
            return True

    except Exception as e:
        await log_info(
            f"Ошибка при обновлении таблицы {table_name} для user_id={user_id}: {e}",
            type_msg="error",
            user_id=user_id,
        )
        return False

    finally:
        if connection:
            await release_connection(connection)

async def delete_user(user_id: int) -> bool:
    """Удаляем пользователя по user_id."""
    connection = None
    try:
        connection = await get_connection()
        await connection.execute('DELETE FROM "users" WHERE user_id = $1;', user_id)
        await log_info(
            f"Удалена строка пользователя user_id={user_id} из таблицы users",
            type_msg="info",
            user_id=user_id,
        )
        return True
    except Exception as e:
        await log_info(
            f"Ошибка удаления пользователя user_id={user_id}: {e}",
            type_msg="error",
            user_id=user_id,
        )
        return False
    finally:
        if connection:
            await release_connection(connection)

async def get_user_data(table_name: str, user_id: int) -> dict | None:
    """
    Асинхронная функция для чтения строки из БД по user_id.

    Args:
        table_name (str): название таблицы
        user_id (int): уникальный идентификатор пользователя (по колонке user_id)

    Returns:
        dict | None: словарь с данными пользователя или None, если не найдено/ошибка
    """
    connection = None
    try:
        connection = await get_connection()

        query = f'SELECT * FROM "{table_name}" WHERE user_id = $1 LIMIT 1;'
        row = await connection.fetchrow(query, user_id)

        if row:
            await log_info(
                f"Данные получены из {table_name} для user_id={user_id}",
                type_msg="info",
                user_id=user_id,
            )
            return dict(row)

        await log_info(
            f"Запись не найдена в {table_name} для user_id={user_id}",
            type_msg="warning",
            user_id=user_id,
        )
        return None

    except Exception as e:
        await log_info(
            f"Ошибка при получении данных из {table_name} для user_id={user_id}: {e}",
            type_msg="error",
            user_id=user_id,
        )
        return None

    finally:
        if connection:
            await release_connection(connection)

async def get_available_drivers(city: str):
    """Список доступных водителей в городе"""
    connection = await get_connection()
    try:
        query = """
            SELECT user_id FROM users
            WHERE role = 'driver'
              AND is_working = TRUE
              AND city = $1
        """
        return await connection.fetch(query, city)
    except Exception as e:
        await log_info(f"Ошибка при получении доступных водителей в {city}: {e}", type_msg="error")
        return []
    finally:
        await release_connection(connection)

async def reserve_order(order_id: int, driver_id: int) -> Optional[Dict]:
    """
    Атомарно принимает заказ:
    SET driver_id, status='accepted' WHERE id=$1 AND driver_id IS NULL AND status='pending'
    Возвращает краткие данные заказа (для сообщений) или None.
    """
    connection = await get_connection()
    try:
        row = await connection.fetchrow(
            """
            UPDATE orders
            SET driver_id = $1, status = 'accepted'
            WHERE order_id = $2 AND driver_id IS NULL AND status = 'pending'
            RETURNING passenger_id, city, address_from, address_to
            """,
            driver_id, order_id
        )
        return dict(row) if row else None
    except Exception as e:
        await log_info(
            f"reserve_order failed: {e}",
            type_msg="error",
            user_id=driver_id,
        )
        return None
    finally:
        await release_connection(connection)


async def set_driver_working(driver_id: int, is_working: bool) -> bool:
    connection = await get_connection()
    try:
        await connection.execute("UPDATE users SET is_working = $1 WHERE user_id = $2", is_working, driver_id)
        return True
    except Exception as e:
        await log_info(
            f"set_driver_working failed: {e}",
            type_msg="error",
            user_id=driver_id,
        )
        return False
    finally:
        await release_connection(connection)


async def fetch_passenger_contact(passenger_id: int) -> Dict:
    connection = await get_connection()
    try:
        row = await connection.fetchrow(
            "SELECT first_name, phone_passenger FROM users WHERE user_id = $1",
            passenger_id
        )
        return dict(row) if row else {"first_name": "-", "phone_passenger": "-"}
    except Exception as e:
        await log_info(
            f"fetch_passenger_contact failed: {e}",
            type_msg="error",
            user_id=passenger_id,
        )
        return {"first_name": "-", "phone_passenger": "-"}
    finally:
        await release_connection(connection)


async def fetch_driver_card(driver_id: int) -> Dict:
    connection = await get_connection()
    try:
        row = await connection.fetchrow(
            """
            SELECT first_name, phone_driver, car_model, car_color, car_number
            FROM users WHERE user_id = $1
            """,
            driver_id
        )
        return dict(row) if row else {
            "first_name": "-", "phone_driver": "-", "car_model": "-", "car_color": "-", "car_number": "-"
        }
    except Exception as e:
        await log_info(
            f"fetch_driver_card failed: {e}",
            type_msg="error",
            user_id=driver_id,
        )
        return {
            "first_name": "-", "phone_driver": "-", "car_model": "-", "car_color": "-", "car_number": "-"
        }
    finally:
        await release_connection(connection)


async def list_other_available_drivers(city: str, exclude_user_id: int) -> List[int]:
    connection = await get_connection()
    try:
        rows = await connection.fetch(
            """
            SELECT user_id FROM users
            WHERE role = 'driver' AND is_working = TRUE AND city = $1 AND user_id != $2
            """,
            city, exclude_user_id
        )
        return [r["user_id"] for r in rows]
    except Exception as e:
        await log_info(
            f"list_other_available_drivers failed: {e}",
            type_msg="error",
            user_id=exclude_user_id,
        )
        return []
    finally:
        await release_connection(connection)


async def cancel_order(order_id: int, initiator_id: int) -> Optional[Dict]:
    """
    Атомарная отмена заказа из ЛЮБОГО активного статуса.
    Ставит status='canceled', инициатора и trip_end=NOW() (момент отмены).
    Возвращает {'passenger_id': int, 'driver_id': Optional[int]} или None.
    """
    connection = await get_connection()
    try:
        row = await connection.fetchrow(
            """
            UPDATE orders
            SET status = 'canceled',
                initiator_id = $2,
                trip_end = NOW()
            WHERE order_id = $1
              AND status IN ('pending','accepted','in_place','come_out','started')
            RETURNING passenger_id, driver_id
            """,
            order_id, initiator_id
        )
        if not row:
            return None
        return {"passenger_id": row["passenger_id"], "driver_id": row["driver_id"]}
    except Exception as e:
        await log_info(
            f"cancel_order failed: {e}",
            type_msg="error",
            user_id=initiator_id,
        )
        return None
    finally:
        await release_connection(connection)

async def get_order_data(order_id: int) -> Optional[Dict]:
    """Вернуть полную строку заказа по order_id."""
    connection = None
    try:
        connection = await get_connection()
        row = await connection.fetchrow('SELECT * FROM orders WHERE order_id = $1 LIMIT 1;', order_id)
        return dict(row) if row else None
    except Exception as e:
        await log_info(f"get_order_data failed: {e}", type_msg="error")
        return None
    finally:
        if connection:
            await release_connection(connection)

async def get_order_message_id(order_id: int) -> Optional[int]:
    """Вернуть message_id служебного сообщения заказа (для reply-треда)."""
    connection = None
    try:
        connection = await get_connection()
        mid = await connection.fetchval('SELECT message_id FROM orders WHERE order_id = $1;', order_id)
        return int(mid) if mid else None
    except Exception as e:
        await log_info(f"get_order_message_id failed: {e}", type_msg="error")
        return None
    finally:
        if connection:
            await release_connection(connection)


async def complete_order(order_id: int, driver_id: int) -> Optional[int]:
    """status='completed', trip_end=NOW()."""
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET status = 'completed',
                   trip_end = NOW()
             WHERE order_id = $1
               AND driver_id = $2
               AND status NOT IN ('canceled','completed')
            RETURNING passenger_id
            """, order_id, driver_id
        )
        return row["passenger_id"] if row else None
    except Exception as e:
        await log_info(
            f"complete_order failed: {e}",
            type_msg="error",
            user_id=driver_id,
        )
        return None
    finally:
        await release_connection(conn)

async def mark_trip_started(order_id: int, driver_id: int) -> bool:
    """status='started', trip_start=NOW()."""
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET status = 'started',
                   trip_start = COALESCE(trip_start, NOW())
             WHERE order_id = $1
               AND driver_id = $2
               AND status NOT IN ('canceled','completed')
            RETURNING order_id
            """, order_id, driver_id
        )
        return bool(row)
    except Exception as e:
        await log_info(
            f"mark_trip_started failed: {e}",
            type_msg="error",
            user_id=driver_id,
        )
        return False
    finally:
        await release_connection(conn)

async def mark_driver_arrived(order_id: int) -> bool:
    """status='in_place', in_place_at=NOW()."""
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET status = 'in_place',
                   in_place_at = COALESCE(in_place_at, NOW())
             WHERE order_id = $1
               AND status NOT IN ('canceled','completed')
            RETURNING order_id
            """, order_id
        )
        return bool(row)
    except Exception as e:
        await log_info(f"mark_driver_arrived failed: {e}", type_msg="error")
        return False
    finally:
        await release_connection(conn)

async def ensure_order_date_now(order_id: int) -> None:
    """
    Если у заказа order_date = NULL (нет DEFAULT в схеме) — проставляет NOW().
    Безопасно, ничего не возвращает.
    """
    conn = await get_connection()
    try:
        await conn.execute(
            """
            UPDATE orders
               SET order_date = COALESCE(order_date, NOW())
             WHERE order_id = $1
            """,
            order_id
        )
    except Exception as e:
        await log_info(f"ensure_order_date_now failed: {e}", type_msg="error")
    finally:
        await release_connection(conn)

async def mark_passenger_comeout(order_id: int) -> bool:
    """status='come_out', come_out_at=NOW()."""
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET status = 'come_out',
                   come_out_at = COALESCE(come_out_at, NOW())
             WHERE order_id = $1
               AND status NOT IN ('canceled','completed')
            RETURNING order_id
            """, order_id
        )
        return bool(row)
    except Exception as e:
        await log_info(f"mark_passenger_comeout failed: {e}", type_msg="error")
        return False
    finally:
        await release_connection(conn)

async def mark_auto_start_hint_sent(order_id: int) -> None:
    """Помечаем момент отправки auto_start_hint."""
    conn = await get_connection()
    try:
        await conn.execute(
            "UPDATE orders SET auto_start_hint_at = COALESCE(auto_start_hint_at, NOW()) WHERE order_id = $1",
            order_id
        )
    except Exception as e:
        await log_info(f"mark_auto_start_hint_sent failed: {e}", type_msg="error")
    finally:
        await release_connection(conn)

# === Сохранение/восстановление снапшота рантайм-переменных ===

async def save_runtime_snapshot(key: str, payload: dict) -> bool:
    """
    UPSERT снапшота (key -> JSONB payload).
    Без asyncpg.Json: сериализуем сами и кастуем к ::jsonb.
    """
    conn = await get_connection()
    try:
        json_ready = _jsonable(payload)
        await conn.execute(
            """
            INSERT INTO bot_runtime(key, payload)
                 VALUES ($1, $2::jsonb)
            ON CONFLICT (key)
            DO UPDATE SET payload = EXCLUDED.payload,
                          updated_at = NOW()
            """,
            key, json.dumps(json_ready, ensure_ascii=False)
        )
        return True
    except Exception as e:
        await log_info(f"save_runtime_snapshot failed: {e}", type_msg="error")
        return False
    finally:
        await release_connection(conn)


async def load_runtime_snapshot(key: str) -> Optional[dict]:
    """
    Читает JSONB из bot_runtime.payload.
    asyncpg возвращает Python-объект (dict/list/...); если это dict — отдаём его.
    """
    conn = await get_connection()
    try:
        row = await conn.fetchrow("SELECT payload FROM bot_runtime WHERE key = $1", key)
        if not row:
            return None
        val = row["payload"]
        return val if isinstance(val, dict) else None
    except Exception as e:
        await log_info(f"load_runtime_snapshot failed: {e}", type_msg="error")
        return None
    finally:
        await release_connection(conn)

async def get_active_order_ids() -> List[int]:
    """
    Вернёт список order_id для активных заказов.
    Активные статусы: pending, accepted, in_place, started.
    """
    connection = await get_connection()
    try:
        rows = await connection.fetch(
            """
            SELECT order_id
              FROM orders
             WHERE status IN ('pending','accepted','in_place','started')
            """
        )
        return [int(r["order_id"]) for r in rows]
    except Exception as e:
        await log_info(f"get_active_order_ids failed: {e}", type_msg="error")
        return []
    finally:
        await release_connection(connection)

async def get_orders_by_statuses(statuses: Iterable[str]) -> List[Dict]:
    """
    Вернёт полные строки заказов по списку статусов.
    """
    connection = await get_connection()
    try:
        rows = await connection.fetch(
            """SELECT * FROM orders WHERE status = ANY($1::text[])""",
            list(statuses)
        )
        return [dict(r) for r in rows]
    except Exception as e:
        await log_info(f"get_orders_by_statuses failed: {e}", type_msg="error")
        return []
    finally:
        await release_connection(connection)

async def get_latest_open_order_id_for_passenger(passenger_id: int) -> Optional[int]:
    """
    Вернёт order_id последнего «живого» заказа пассажира или None.
    Активные статусы: pending, accepted, in_place, come_out, started.
    """
    connection = None
    try:
        connection = await get_connection()
        order_id = await connection.fetchval(
            """
            SELECT order_id
              FROM orders
             WHERE passenger_id = $1
               AND COALESCE(status,'pending') IN
                   ('pending','accepted','in_place','come_out','started')
             ORDER BY order_date DESC NULLS LAST, order_id DESC
             LIMIT 1
            """,
            passenger_id
        )

        if order_id:
            await log_info(
                f"Найден активный заказ для passenger_id={passenger_id}: order_id={order_id}",
                type_msg="info",
                user_id=passenger_id,
            )
            return int(order_id)

        await log_info(
            f"Активный заказ для passenger_id={passenger_id} не найден.",
            type_msg="warning",
            user_id=passenger_id,
        )
        return None

    except Exception as e:
        await log_info(
            f"get_latest_open_order_id_for_passenger failed: {e}",
            type_msg="error",
            user_id=passenger_id,
        )
        return None

    finally:
        if connection:
            await release_connection(connection)

async def get_active_order_for_driver(driver_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает активный заказ для водителя либо None."""
    connection = None
    try:
        connection = await get_connection()
        row = await connection.fetchrow(
            """
            SELECT *
              FROM orders
             WHERE driver_id = $1
               AND status IN ('accepted','in_place','come_out','started','awaiting_fee')
             ORDER BY order_date DESC NULLS LAST, order_id DESC
             LIMIT 1
            """,
            driver_id,
        )
        if row:
            await log_info(
                "[db_utils.get_active_order_for_driver] найден активный заказ",
                type_msg="info",
                user_id=driver_id,
                order_id=row["order_id"],
            )
            return dict(row)
        await log_info(
            "[db_utils.get_active_order_for_driver] активный заказ не найден",
            type_msg="warning",
            user_id=driver_id,
        )
        return None
    except Exception as error:
        await log_info(
            f"[db_utils.get_active_order_for_driver] ошибка: {error}",
            type_msg="error",
            user_id=driver_id,
        )
        return None
    finally:
        if connection:
            await release_connection(connection)


async def list_available_orders_for_driver(
    city: str | None,
    limit: int = 20,
    *,
    exclude_user_id: int | None = None,
) -> List[Dict[str, Any]]:
    """Возвращает список доступных заказов для водителя."""

    connection = None
    try:
        connection = await get_connection()
        clauses = ["status = 'pending'", "driver_id IS NULL"]
        params: list[Any] = []

        if city:
            params.append(city)
            clauses.append(f"COALESCE(city, '') = ${len(params)}")

        if exclude_user_id is not None:
            params.append(exclude_user_id)
            clauses.append(f"COALESCE(passenger_id, 0) <> ${len(params)}")

        params.append(limit)
        where_sql = " AND ".join(clauses)
        query = (
            f"""
            SELECT *
              FROM orders
             WHERE {where_sql}
             ORDER BY scheduled_at NULLS LAST, order_date DESC NULLS LAST, order_id DESC
             LIMIT ${len(params)}
            """
        )

        rows = await connection.fetch(query, *params)
        result = [dict(row) for row in rows]
        await log_info(
            "[db_utils.list_available_orders_for_driver] получены доступные заказы",
            type_msg="info",
            user_id=exclude_user_id,
            count=len(result),
        )
        return result
    except Exception as error:
        await log_info(
            f"[db_utils.list_available_orders_for_driver] ошибка: {error}",
            type_msg="error",
            user_id=exclude_user_id,
        )
        return []
    finally:
        if connection:
            await release_connection(connection)


async def list_future_orders_for_passenger(passenger_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Возвращает список будущих поездок пассажира."""

    connection = None
    try:
        connection = await get_connection()
        rows = await connection.fetch(
            """
            SELECT *
              FROM orders
             WHERE passenger_id = $1
               AND scheduled_at IS NOT NULL
               AND scheduled_at > NOW()
             ORDER BY scheduled_at ASC
             LIMIT $2
            """,
            passenger_id,
            limit,
        )
        result = [dict(row) for row in rows]
        await log_info(
            "[db_utils.list_future_orders_for_passenger] загружено поездок",
            type_msg="info",
            user_id=passenger_id,
            count=len(result),
        )
        return result
    except Exception as error:
        await log_info(
            f"[db_utils.list_future_orders_for_passenger] ошибка: {error}",
            type_msg="error",
            user_id=passenger_id,
        )
        return []
    finally:
        if connection:
            await release_connection(connection)


async def list_order_history_for_user(
    user_id: int,
    *,
    role: Literal["passenger", "driver"],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Возвращает историю поездок пользователя."""

    connection = None
    column = "passenger_id" if role == "passenger" else "driver_id"
    try:
        connection = await get_connection()
        query = f"""
            SELECT *
              FROM orders
             WHERE {column} = $1
               AND status IN ('completed','canceled')
             ORDER BY COALESCE(trip_end, order_date) DESC NULLS LAST, order_id DESC
             LIMIT $2
        """
        rows = await connection.fetch(query, user_id, limit)
        result = [dict(row) for row in rows]
        await log_info(
            "[db_utils.list_order_history_for_user] получена история поездок",
            type_msg="info",
            user_id=user_id,
            role=role,
            count=len(result),
        )
        return result
    except Exception as error:
        await log_info(
            f"[db_utils.list_order_history_for_user] ошибка: {error}",
            type_msg="error",
            user_id=user_id,
        )
        return []
    finally:
        if connection:
            await release_connection(connection)


async def get_driver_stats_summary(driver_id: int) -> Dict[str, Any]:
    """Собирает агрегированные метрики по заказам водителя."""

    connection = None
    try:
        connection = await get_connection()
        row = await connection.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'canceled') AS canceled,
                COUNT(*) FILTER (WHERE status IN ('accepted','in_place','come_out','started','awaiting_fee')) AS active,
                COALESCE(SUM(commission), 0) AS total_commission,
                COALESCE(SUM(cost), 0) AS total_revenue
              FROM orders
             WHERE driver_id = $1
            """,
            driver_id,
        )
        payload = dict(row) if row else {}
        stats = {
            "completed": int(payload.get("completed") or 0),
            "canceled": int(payload.get("canceled") or 0),
            "active": int(payload.get("active") or 0),
            "total_commission": float(payload.get("total_commission") or 0),
            "total_revenue": float(payload.get("total_revenue") or 0),
        }
        await log_info(
            "[db_utils.get_driver_stats_summary] статистика собрана",
            type_msg="info",
            user_id=driver_id,
            stats=stats,
        )
        return stats
    except Exception as error:
        await log_info(
            f"[db_utils.get_driver_stats_summary] ошибка: {error}",
            type_msg="error",
            user_id=driver_id,
        )
        return {
            "completed": 0,
            "canceled": 0,
            "active": 0,
            "total_commission": 0.0,
            "total_revenue": 0.0,
        }
    finally:
        if connection:
            await release_connection(connection)

async def get_user_theme(user_id: int) -> str | None:
    """
    Возвращает 'light' / 'dark' или None, если записи нет/пусто.
    """
    conn = None
    try:
        conn = await get_connection()
        row = await conn.fetchrow(
            'SELECT theme_mode FROM users WHERE user_id = $1', user_id
        )
        if not row:
            return None
        theme = row['theme_mode']
        return theme if theme in ('light', 'dark') else None
    finally:
        if conn:
            await release_connection(conn)
