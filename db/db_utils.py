from db.db_table_init import get_connection, release_connection
from log.log import log_info
from typing import Optional, Dict, List, Union, Tuple
import json
from typing import Iterable
from datetime import datetime, date, time
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
